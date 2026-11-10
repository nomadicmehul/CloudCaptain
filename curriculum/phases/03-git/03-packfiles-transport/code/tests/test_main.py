import contextlib
import hashlib
import http.client
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import main  # noqa: E402

GIT = shutil.which("git")
RESULT = {"content-type": "application/x-git-upload-pack-result"}
ADVERTISEMENT = {"content-type": "application/x-git-upload-pack-advertisement"}
SERVER_CAPS = ["agent=git/2.99.0", "ls-refs=unborn", "fetch=shallow wait-for-done filter", "server-option", "object-format=sha1"]


def http_backend_available():
    if not GIT:
        return False
    exec_path = subprocess.run([GIT, "--exec-path"], capture_output=True, text=True).stdout.strip()
    return os.path.exists(os.path.join(exec_path, "git-http-backend"))


def run(argv, transport=None):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main.main(argv, transport=transport)
    return code, out.getvalue(), err.getvalue()


def pseudo_random(n, seed="x"):
    blocks = []
    i = 0
    while sum(len(b) for b in blocks) < n:
        blocks.append(hashlib.sha256(f"{seed}{i}".encode()).digest())
        i += 1
    return b"".join(blocks)[:n]


def write_tree(repo, entries):
    data = b"".join(b"%s %s\x00" % (mode.encode(), name.encode()) + bytes.fromhex(oid) for mode, name, oid in sorted(entries, key=lambda e: e[1]))
    return repo.write("tree", data)


def write_commit(repo, tree, parents, message, when):
    lines = [f"tree {tree}"] + [f"parent {p}" for p in parents]
    lines += [f"author Dev <dev@example.invalid> {when} +0000", f"committer Dev <dev@example.invalid> {when} +0000", "", message, ""]
    return repo.write("commit", "\n".join(lines).encode())


def commit_file(repo, text, parents, when, name="a.txt"):
    blob = repo.write("blob", text.encode())
    return write_commit(repo, write_tree(repo, [("100644", name, blob)]), parents, f"commit at {when}", when)


def sideband(pack):
    return b"".join(main.pkt_line(b"\x01" + pack[i:i + 1000]) for i in range(0, len(pack), 1000))


class FakeServer:
    def __init__(self, refs, pack=b"", caps=None, shallow=(), known=(), service_line=False):
        self.refs = refs
        self.pack = pack
        self.caps = SERVER_CAPS if caps is None else caps
        self.shallow = list(shallow)
        self.known = set(known)
        self.service_line = service_line
        self.requests = []

    def __call__(self, method, url, headers, body):
        if method == "GET":
            out = main.pkt_line("# service=git-upload-pack\n") + main.FLUSH if self.service_line else b""
            out += main.pkt_line("version 2\n") + b"".join(main.pkt_line(c + "\n") for c in self.caps) + main.FLUSH
            return 200, dict(ADVERTISEMENT), out
        packets = main.read_packets(body)
        delim = next(i for i, p in enumerate(packets) if p.kind == "delim")
        command = packets[0].text().split("=", 1)[1]
        args = [p.text() for p in packets[delim + 1:] if p.kind == "data"]
        self.requests.append({"command": command, "arguments": args, "headers": headers, "url": url})
        if command == "ls-refs":
            return 200, dict(RESULT), b"".join(main.pkt_line(r + "\n") for r in self.refs) + main.FLUSH
        haves = [a[5:] for a in args if a.startswith("have ")]
        out = b""
        if "done" not in args:
            acks = [h for h in haves if h in self.known]
            out += main.pkt_line("acknowledgments\n")
            out += b"".join(main.pkt_line(f"ACK {a}\n") for a in acks) if acks else main.pkt_line("NAK\n")
            if not acks:
                return 200, dict(RESULT), out + main.FLUSH
            out += main.pkt_line("ready\n") + main.DELIM
        if self.shallow and any(a.startswith("deepen ") for a in args):
            out += main.pkt_line("shallow-info\n") + b"".join(main.pkt_line(f"shallow {s}\n") for s in self.shallow) + main.DELIM
        out += main.pkt_line("packfile\n") + main.pkt_line(b"\x02Enumerating objects: done.\n") + sideband(self.pack) + main.FLUSH
        return 200, dict(RESULT), out

    def fetches(self):
        return [r["arguments"] for r in self.requests if r["command"] == "fetch"]


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)


class PktLineTests(unittest.TestCase):
    def test_framing_and_special_packets(self):
        self.assertEqual(main.pkt_line("version 2\n"), b"000eversion 2\n")
        self.assertEqual(main.pkt_line(b""), b"0004")
        packets = main.read_packets(b"000eversion 2\n" + main.DELIM + main.RESPONSE_END + main.FLUSH)
        self.assertEqual([p.kind for p in packets], ["data", "delim", "response-end", "flush"])
        self.assertEqual([p.show() for p in packets], ["version 2", "0001", "0002", "0000"])
        self.assertEqual(main.Packet("data", b"PACK\x00\x00\x00\x02").show(), "PACK ...")

    def test_malformed_pkt_lines_are_rejected_with_the_byte_position(self):
        with self.assertRaisesRegex(main.ProtocolError, "over the limit"):
            main.pkt_line(b"x" * (main.MAX_PKT_PAYLOAD + 1))
        self.assertEqual(len(main.pkt_line(b"x" * main.MAX_PKT_PAYLOAD)), 65520)
        for data, message in ((b"00zzabc", "four hex digits"), (b"0009abc", "promises 5 bytes"), (b"0003", "invalid pkt-line length 3"), (b"00", "ends inside")):
            with self.assertRaisesRegex(main.ProtocolError, message):
                main.read_packets(data)


class VarintTests(unittest.TestCase):
    def test_object_header_packs_type_and_size_little_endian(self):
        self.assertEqual(main.encode_entry_header(main.OBJ_BLOB, 300), bytes([0xBC, 0x12]))
        self.assertEqual(main.encode_entry_header(main.OBJ_COMMIT, 15), bytes([0x1F]))
        self.assertEqual(main.encode_entry_header(main.OBJ_TREE, 16), bytes([0xA0, 0x01]))
        for type_code in (1, 2, 3, 4, 6, 7):
            for size in (0, 15, 16, 127, 128, 2047, 2048, 10 ** 6, 2 ** 40):
                encoded = main.encode_entry_header(type_code, size)
                self.assertEqual(main.decode_entry_header(encoded + b"tail", 0), (type_code, size, len(encoded)))

    def test_ofs_delta_offsets_use_the_plus_one_encoding(self):
        known = {1: b"\x01", 127: b"\x7f", 128: b"\x80\x00", 16511: b"\xff\x7f", 16512: b"\x80\x80\x00"}
        for distance, encoded in known.items():
            self.assertEqual(main.encode_ofs(distance), encoded, distance)
        for distance in list(known) + [2 ** 20 + 7, 2 ** 31 + 5]:
            encoded = main.encode_ofs(distance)
            self.assertEqual(main.decode_ofs(encoded, 0), (distance, len(encoded)))

    def test_delta_sizes_are_plain_base_128(self):
        self.assertEqual(main.encode_size(0), b"\x00")
        self.assertEqual(main.encode_size(127), b"\x7f")
        self.assertEqual(main.encode_size(128), b"\x80\x01")
        self.assertEqual(main.encode_size(300), b"\xac\x02")
        self.assertEqual(main.decode_size(b"\xac\x02rest", 0), (300, 2))


class DeltaTests(unittest.TestCase):
    def test_a_hand_built_delta_of_copies_and_an_insert(self):
        base = b"hello world"
        delta = main.encode_size(11) + main.encode_size(17) + main.copy_op(0, 6) + bytes([5]) + b"there" + main.copy_op(5, 6)
        self.assertEqual(main.copy_op(0, 6), b"\x90\x06")
        self.assertEqual(main.copy_op(5, 6), b"\x91\x05\x06")
        self.assertEqual(main.copy_op(0x010203, 0x10000), b"\x87\x03\x02\x01")
        self.assertEqual(main.apply_delta(base, delta), b"hello there world")
        _, _, ops = main.parse_delta(delta)
        self.assertEqual([(o.op, o.offset, o.size) for o in ops], [("copy", 0, 6), ("insert", 0, 5), ("copy", 5, 6)])

    def test_a_copy_without_size_bytes_copies_64_kib(self):
        base = pseudo_random(70000)
        delta = main.encode_size(70000) + main.encode_size(0x10000) + b"\x80"
        self.assertEqual(main.apply_delta(base, delta), base[:0x10000])

    def test_bad_deltas_are_rejected(self):
        base = b"0123456789"
        cases = {
            main.encode_size(10) + main.encode_size(1) + b"\x00": "opcode 0 is reserved",
            main.encode_size(10) + main.encode_size(5) + main.copy_op(8, 5): "runs past the 10-byte base",
            main.encode_size(9) + main.encode_size(1) + main.copy_op(0, 1): "expects a base of 9 bytes",
            main.encode_size(10) + main.encode_size(3) + main.copy_op(0, 2): "produced 2 bytes",
            main.encode_size(10) + main.encode_size(4) + b"\x04ab": "runs past the end of the delta",
            b"\x8a": "ends inside its size header",
        }
        for delta, message in cases.items():
            with self.assertRaisesRegex(main.PackError, message):
                main.apply_delta(base, delta)

    def test_make_delta_copies_what_is_shared_and_inserts_the_rest(self):
        base = "".join(f"line {i:04d} of the configuration file\n" for i in range(200)).encode()
        target = base.replace(b"line 0100 of", b"line 0100 changed in") + b"one more line\n"
        delta = main.make_delta(base, target)
        self.assertEqual(main.apply_delta(base, delta), target)
        self.assertLess(len(delta), 64)
        _, _, ops = main.parse_delta(delta)
        self.assertEqual((ops[0].op, ops[0].offset, ops[0].size), ("copy", 0, base.index(b"of", 3600)))
        self.assertEqual([o.data for o in ops if o.op == "insert"], [b"changed in", b"one more line\n"])
        self.assertEqual(sum(o.size for o in ops if o.op == "copy"), len(target) - 24)

    def test_long_matches_are_split_into_copies_of_at_most_64_kib(self):
        base = pseudo_random(204800)
        _, _, ops = main.parse_delta(main.make_delta(base, base))
        self.assertEqual([(o.op, o.offset, o.size) for o in ops], [("copy", 0, 65536), ("copy", 65536, 65536), ("copy", 131072, 65536), ("copy", 196608, 8192)])

    def test_unrelated_data_becomes_inserts_of_at_most_127_bytes(self):
        base, target = pseudo_random(1000, "a"), pseudo_random(300, "b")
        delta = main.make_delta(base, target)
        _, _, ops = main.parse_delta(delta)
        self.assertEqual({o.op for o in ops}, {"insert"})
        self.assertEqual([o.size for o in ops], [127, 127, 46])
        self.assertEqual(main.apply_delta(base, delta), target)


def similar_blobs():
    base = "".join(f"setting_{i} = {i * 7}\n" for i in range(120))
    return [(main.object_id("blob", t.encode()), "blob", t.encode(), f"conf/v{n}.ini") for n, t in enumerate([base, base.replace("= 70\n", "= 71\n"), base + "extra = 1\n"])]


class PackTests(unittest.TestCase):
    def test_write_then_parse_with_ofs_and_ref_deltas(self):
        objects = similar_blobs() + [(main.object_id("blob", b"tiny\n"), "blob", b"tiny\n", "tiny.txt")]
        plans = main.plan_pack(objects)
        self.assertEqual(sum(1 for p in plans if p.base), 2)
        for ofs in (True, False):
            pack, records, checksum = main.write_pack(plans, ofs_delta=ofs)
            self.assertEqual(pack[:12], b"PACK" + struct.pack(">II", 2, 4))
            self.assertEqual(pack[-20:].hex(), checksum)
            indexed = main.index_pack(pack)
            self.assertEqual({oid: data for oid, (_, data) in indexed.objects.items()}, {o[0]: o[2] for o in objects})
            kinds = sorted(e.type_code for e in indexed.entries)
            self.assertEqual(kinds, [main.OBJ_BLOB, main.OBJ_BLOB] + [main.OBJ_OFS_DELTA if ofs else main.OBJ_REF_DELTA] * 2)
            self.assertEqual(sorted(indexed.records()), sorted(records))
            self.assertEqual({e.depth for e in indexed.entries if e.base}, {1})

    def test_corruption_is_detected(self):
        pack, _, _ = main.write_pack(main.plan_pack(similar_blobs()))
        flipped = bytearray(pack)
        flipped[40] ^= 0xFF
        with self.assertRaisesRegex(main.PackError, "checksum mismatch"):
            main.parse_pack(bytes(flipped))
        with self.assertRaisesRegex(main.PackError, "starts with b'PACK'"):
            main.parse_pack(b"KCAP" + pack[4:])
        bad_version = b"PACK" + struct.pack(">II", 4, 3) + pack[12:-20]
        with self.assertRaisesRegex(main.PackError, "version 4"):
            main.parse_pack(bad_version + hashlib.sha1(bad_version).digest())
        more = b"PACK" + struct.pack(">II", 2, 5) + pack[12:-20]
        with self.assertRaisesRegex(main.PackError, "promises 5 objects"):
            main.parse_pack(more + hashlib.sha1(more).digest())

    def test_a_thin_pack_resolves_only_with_the_local_base(self):
        base_oid, _, base, _ = similar_blobs()[0]
        target = base + b"appended\n"
        plan = main.PackPlan(main.object_id("blob", target), "blob", target, base=base_oid, delta=main.make_delta(base, target))
        pack, _, _ = main.write_pack([plan], ofs_delta=False)
        with self.assertRaisesRegex(main.PackError, f"bases not in the pack: {base_oid}"):
            main.index_pack(pack)
        indexed = main.index_pack(pack, lambda oid: ("blob", base) if oid == base_oid else None)
        self.assertEqual(indexed.thin_bases, [base_oid])
        self.assertEqual(indexed.objects[plan.oid], ("blob", target))


class IdxTests(unittest.TestCase):
    RECORDS = [("ff" + "1" * 38, 0xDEADBEEF, 400), ("00" + "2" * 38, 7, 12), ("0a" + "3" * 38, 9, 200)]

    def test_version_2_layout(self):
        idx = main.write_idx(self.RECORDS, "ab" * 20)
        self.assertEqual(idx[:8], b"\xfftOc\x00\x00\x00\x02")
        fanout = struct.unpack(">256I", idx[8:1032])
        self.assertEqual((fanout[0x00], fanout[0x09], fanout[0x0A], fanout[0xFE], fanout[0xFF]), (1, 1, 2, 2, 3))
        names = [idx[1032 + 20 * i:1052 + 20 * i].hex() for i in range(3)]
        self.assertEqual(names, sorted(r[0] for r in self.RECORDS))
        self.assertEqual(struct.unpack(">3I", idx[1092:1104]), (7, 9, 0xDEADBEEF))
        self.assertEqual(struct.unpack(">3I", idx[1104:1116]), (12, 200, 400))
        self.assertEqual(idx[1116:1136].hex(), "ab" * 20)
        self.assertEqual(idx[-20:], hashlib.sha1(idx[:-20]).digest())
        self.assertEqual(len(idx), 1136 + 20)
        records, checksum = main.read_idx(idx)
        self.assertEqual(([(r.oid, r.crc32, r.offset) for r in records], checksum), (sorted(self.RECORDS), "ab" * 20))

    def test_offsets_past_2_gib_go_to_the_64_bit_table(self):
        records = [("10" + "0" * 38, 1, 2 ** 31 + 10), ("20" + "0" * 38, 2, 12), ("30" + "0" * 38, 3, 2 ** 33)]
        idx = main.write_idx(records, "cd" * 20)
        self.assertEqual(struct.unpack(">3I", idx[1032 + 60 + 12:1032 + 60 + 24]), (0x80000000, 12, 0x80000001))
        self.assertEqual(struct.unpack(">2Q", idx[1116:1132]), (2 ** 31 + 10, 2 ** 33))
        self.assertEqual(len(idx), 1132 + 40)
        self.assertEqual([r.offset for r in main.read_idx(idx)[0]], [2 ** 31 + 10, 12, 2 ** 33])

    def test_damaged_indexes_are_refused(self):
        idx = bytearray(main.write_idx(self.RECORDS, "ab" * 20))
        idx[1040] ^= 1
        with self.assertRaisesRegex(main.PackError, "checksum mismatch"):
            main.read_idx(bytes(idx))
        with self.assertRaisesRegex(main.PackError, "version 1 indexes"):
            main.read_idx(b"\x00\x00\x00\x00" * 300)


class ProtocolTests(unittest.TestCase):
    def test_capability_advertisement_with_and_without_the_service_line(self):
        for service_line in (False, True):
            server = FakeServer([], service_line=service_line)
            trace = main.Trace()
            remote = main.Remote("http://example.invalid/app.git/", server, trace)
            caps = remote.discover()
            self.assertEqual(caps["fetch"], "shallow wait-for-done filter")
            self.assertEqual(remote.features("fetch"), {"shallow", "wait-for-done", "filter"})
            self.assertEqual(remote.features("ls-refs"), {"unborn"})
            self.assertIn("packet: minipack< version 2", trace.lines)

    def test_dumb_v0_and_sha256_servers_are_refused_with_a_reason(self):
        def dumb(method, url, headers, body):
            return 200, {"content-type": "text/plain"}, b"0123 refs/heads/main\n"

        def v0(method, url, headers, body):
            return 200, dict(ADVERTISEMENT), main.pkt_line("a" * 40 + " HEAD\x00multi_ack side-band-64k\n") + main.FLUSH

        def missing(method, url, headers, body):
            return 404, {}, b"Not Found"

        cases = [(dumb, main.ProtocolError, "dumb HTTP server"), (v0, main.ProtocolError, "protocol v0/v1"), (FakeServer([], caps=["object-format=sha256", "fetch="]), main.ProtocolError, "sha256"), (missing, main.InputError, "HTTP 404")]
        for transport, error, message in cases:
            with self.assertRaisesRegex(error, message):
                main.Remote("http://example.invalid/app.git", transport).discover()
        with self.assertRaisesRegex(main.InputError, "smart HTTP only"):
            main.Remote("git@github.com:org/app.git")

    def test_ls_refs_request_bytes_and_ref_attributes(self):
        oid, tag, peeled = "1" * 40, "2" * 40, "3" * 40
        server = FakeServer([f"{oid} HEAD symref-target:refs/heads/main", f"{oid} refs/heads/main", f"{tag} refs/tags/v1 peeled:{peeled}", "unborn refs/heads/empty"])
        refs = main.Remote("http://example.invalid/app.git", server).ls_refs()
        self.assertEqual([(r.name, r.oid, r.symref_target, r.peeled) for r in refs], [("HEAD", oid, "refs/heads/main", ""), ("refs/heads/main", oid, "", ""), ("refs/tags/v1", tag, "", peeled), ("refs/heads/empty", "", "", "")])
        request = server.requests[0]
        self.assertEqual(request["url"], "http://example.invalid/app.git/git-upload-pack")
        self.assertEqual(request["headers"]["Git-Protocol"], "version=2")
        self.assertEqual(request["headers"]["Content-Type"], "application/x-git-upload-pack-request")
        self.assertEqual(request["arguments"], ["peel", "symrefs", "unborn", "ref-prefix refs/heads/", "ref-prefix refs/tags/", "ref-prefix HEAD"])

    def test_fetch_arguments_follow_gits_order(self):
        args = main.FetchArgs(["a" * 40], haves=["b" * 40], done=True, depth=3, shallow=["c" * 40], filter_spec="blob:none", include_tag=True)
        self.assertEqual(main.fetch_arguments(args), ["no-progress", "include-tag", "ofs-delta", "shallow " + "c" * 40, "deepen 3", "filter blob:none", "want " + "a" * 40, "have " + "b" * 40, "done"])
        self.assertEqual(main.fetch_arguments(main.FetchArgs(["a" * 40], done=False)), ["no-progress", "ofs-delta", "want " + "a" * 40])

    def test_fetch_response_sections_and_sideband_channels(self):
        oid = "4" * 40
        body = (main.pkt_line("shallow-info\n") + main.pkt_line(f"shallow {oid}\n") + main.DELIM + main.pkt_line("packfile\n")
                + main.pkt_line(b"\x02Counting objects: 3\n") + main.pkt_line(b"\x01PACK-part-1") + main.pkt_line(b"\x01-part-2") + main.FLUSH)
        trace = main.Trace()
        result = main.parse_fetch_response(main.PktReader(body, trace.received), trace)
        self.assertEqual((result.sections, result.shallow, result.pack, result.progress), (["shallow-info", "packfile"], [oid], b"PACK-part-1-part-2", ["Counting objects: 3"]))
        self.assertEqual(trace.lines[-3:], ["packet: sideband< Counting objects: 3", "packet: sideband< PACK ...", "packet: sideband< 0000"])
        acks = main.parse_fetch_response(main.PktReader(main.pkt_line("acknowledgments\n") + main.pkt_line(f"ACK {oid}\n") + main.pkt_line("ready\n") + main.DELIM + main.pkt_line("packfile\n") + main.pkt_line(b"\x01PACK") + main.FLUSH))
        self.assertEqual((acks.acks, acks.ready, acks.pack), ([oid], True, b"PACK"))
        nak = main.parse_fetch_response(main.PktReader(main.pkt_line("acknowledgments\n") + main.pkt_line("NAK\n") + main.FLUSH))
        self.assertEqual((nak.nak, nak.pack), (True, b""))
        with self.assertRaisesRegex(main.ProtocolError, "server error: upload-pack: not our ref"):
            main.parse_fetch_response(main.PktReader(main.pkt_line("packfile\n") + main.pkt_line(b"\x03upload-pack: not our ref\n")))
        with self.assertRaisesRegex(main.ProtocolError, "server error: access denied"):
            main.parse_fetch_response(main.PktReader(main.pkt_line("ERR access denied\n")))

    def test_parse_trace_reads_git_and_minipack_logs_alike(self):
        git_log = [
            "01:41:56.128089 pkt-line.c:85           packet:        clone> command=ls-refs",
            "01:41:56.128101 pkt-line.c:85           packet:          git< command=ls-refs",
            "01:41:56.128104 pkt-line.c:85           packet:        clone> agent=git/2.56.0",
            "01:41:56.128119 pkt-line.c:85           packet:        clone> 0001",
            "01:41:56.128129 pkt-line.c:85           packet:        clone> peel",
            "01:41:56.128185 pkt-line.c:85           packet:        clone> 0000",
            "01:41:56.153437 pkt-line.c:85           packet:        clone> command=fetch",
            "01:41:56.153454 pkt-line.c:85           packet:        clone> 0001",
            "01:41:56.153458 pkt-line.c:85           packet:        clone> thin-pack",
            "01:41:56.153485 pkt-line.c:85           packet:        clone> done",
            "01:41:56.153488 pkt-line.c:85           packet:        clone> 0000",
            "01:41:56.187763 pkt-line.c:85           packet:        clone< packfile",
        ]
        self.assertEqual(main.parse_trace(git_log), [
            {"command": "ls-refs", "capabilities": ["agent=git/2.56.0"], "arguments": ["peel"]},
            {"command": "fetch", "capabilities": [], "arguments": ["thin-pack", "done"]},
        ])
        mine = ["packet: minipack> command=fetch", "packet: minipack> 0001", "packet: minipack> want " + "a" * 40, "packet: minipack> 0000"]
        self.assertEqual(main.parse_trace(mine, "minipack"), [{"command": "fetch", "capabilities": [], "arguments": ["want " + "a" * 40]}])


class RepositoryTests(TempDirTest):
    def test_loose_objects_refs_shallow_and_config(self):
        repo = main.Repository.init(self.path("r"))
        oid = repo.write("blob", b"hello\n")
        self.assertEqual(oid, "ce013625030ba8dba906f756967f9e9ca394464a")
        self.assertEqual(repo.read(oid), ("blob", b"hello\n"))
        with self.assertRaises(main.MissingObject):
            repo.read("0" * 40)
        repo.write_ref("refs/heads/main", oid)
        repo.write_symref("HEAD", "refs/heads/main")
        with open(self.path("r", ".git", "packed-refs"), "w") as handle:
            handle.write("# pack-refs with: peeled\n" + "1" * 40 + " refs/tags/v1\n^" + "2" * 40 + "\n")
        self.assertEqual(repo.refs(), {"refs/heads/main": oid, "refs/tags/v1": "1" * 40})
        self.assertEqual((repo.read_ref("HEAD"), repo.symref("HEAD")), (oid, "refs/heads/main"))
        repo.write_shallow({oid})
        self.assertEqual(repo.shallow(), {oid})
        repo.write_shallow(set())
        self.assertFalse(os.path.exists(self.path("r", ".git", "shallow")))
        config = main.parse_config('[core]\n\tbare = false\n[remote "origin"]\n\turl = http://h/a.git\n\tpromisor = true\n[Branch "main"]\n\tMerge = refs/heads/main\n')
        self.assertEqual((config["remote.origin.url"], config["remote.origin.promisor"], config["branch.main.merge"]), ("http://h/a.git", "true", "refs/heads/main"))
        self.assertEqual(main.map_ref(main.DEFAULT_REFSPEC, "refs/heads/feature/x"), "refs/remotes/origin/feature/x")
        self.assertIsNone(main.map_ref("+refs/heads/main:refs/remotes/origin/main", "refs/heads/topic"))

    def test_walk_stops_at_shallow_commits_and_can_report_missing_blobs(self):
        repo = main.Repository.init(self.path("r"))
        first = commit_file(repo, "one\n", [], 1000)
        second = commit_file(repo, "two\n", [first], 2000)
        third = commit_file(repo, "three\n", [second], 3000)
        walked = main.walk_objects(repo, [third])
        self.assertEqual([w.type for w in walked], ["commit"] * 3 + ["tree", "blob"] * 3)
        self.assertEqual([w.oid for w in walked[:3]], [third, second, first])
        self.assertEqual([w.path for w in walked if w.type == "blob"], ["a.txt"] * 3)
        shallow = main.walk_objects(repo, [third], shallow={second})
        self.assertEqual([w.oid for w in shallow if w.type == "commit"], [third, second])
        blob = main.object_id("blob", b"three\n")
        os.remove(repo.loose_path(blob))
        with self.assertRaises(main.MissingObject):
            main.walk_objects(repo, [third])
        self.assertTrue(next(w for w in main.walk_objects(repo, [third], allow_missing=True) if w.oid == blob).missing)


class FakeCloneTests(TempDirTest):
    def server_repo(self):
        server = main.Repository.init(self.path("server"))
        c1 = commit_file(server, "one\n", [], 1000)
        c2 = commit_file(server, "two\n", [c1], 2000)
        refs = [f"{c2} HEAD symref-target:refs/heads/main", f"{c2} refs/heads/main", f"{c1} refs/tags/v1"]
        return server, c1, c2, refs

    def test_full_clone_writes_loose_objects_refs_and_config(self):
        server, c1, c2, refs = self.server_repo()
        fake = FakeServer(refs, main.pack_repository(server, [c2])[0])
        code, out, err = run(["clone", "http://example.invalid/app.git", self.path("client"), "--json"], fake)
        self.assertEqual(code, 0, err)
        summary = json.loads(out)
        self.assertEqual(summary["refs"], {"refs/heads/main": c2, "refs/remotes/origin/main": c2, "refs/tags/v1": c1})
        self.assertEqual(fake.fetches(), [["no-progress", "ofs-delta", f"want {c2}", f"want {c1}", "done"]])
        self.assertEqual([r["command"] for r in summary["requests"]], ["ls-refs", "fetch"])
        client = main.Repository(self.path("client"))
        self.assertEqual(client.all_objects(), server.all_objects())
        self.assertEqual((client.symref("HEAD"), client.symref("refs/remotes/origin/HEAD")), ("refs/heads/main", "refs/remotes/origin/main"))
        self.assertEqual(client.config()["remote.origin.fetch"], main.DEFAULT_REFSPEC)
        self.assertEqual(summary["pack"]["storage"], "loose")
        code, _, err = run(["clone", "http://example.invalid/app.git", self.path("client")], fake)
        self.assertEqual(code, 2)
        self.assertIn("not an empty directory", err)

    def test_shallow_clone_is_single_branch_and_records_the_boundary(self):
        server, c1, c2, refs = self.server_repo()
        plans = main.plan_pack([(w.oid, w.type, server.read(w.oid)[1], w.path) for w in main.walk_objects(server, [c2], shallow={c2})])
        fake = FakeServer(refs, main.write_pack(plans)[0], shallow=[c2])
        code, out, err = run(["clone", "http://example.invalid/app.git", self.path("client"), "--depth", "1", "--json"], fake)
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.fetches(), [["no-progress", "include-tag", "ofs-delta", "deepen 1", f"want {c2}", "done"]])
        client = main.Repository(self.path("client"))
        self.assertEqual(client.shallow(), {c2})
        self.assertNotIn("refs/tags/v1", json.loads(out)["refs"])
        self.assertFalse(client.has(c1))
        self.assertEqual(client.config()["remote.origin.fetch"], "+refs/heads/main:refs/remotes/origin/main")

    def test_partial_clone_keeps_a_promisor_pack_or_warns_when_the_server_cannot_filter(self):
        server, c1, c2, refs = self.server_repo()
        plans = main.plan_pack([(w.oid, w.type, server.read(w.oid)[1], w.path) for w in main.walk_objects(server, [c2]) if w.type != "blob"])
        fake = FakeServer(refs, main.write_pack(plans)[0])
        code, out, err = run(["clone", "http://example.invalid/app.git", self.path("partial"), "--filter", "blob:none", "--json"], fake)
        self.assertEqual(code, 0, err)
        self.assertIn("filter blob:none", fake.fetches()[0])
        pack_file = json.loads(out)["pack"]["pack_file"]
        self.assertTrue(os.path.exists(pack_file[:-5] + ".promisor") and os.path.exists(pack_file[:-5] + ".idx"))
        config = main.Repository(self.path("partial")).config()
        self.assertEqual((config["core.repositoryformatversion"], config["remote.origin.promisor"], config["remote.origin.partialclonefilter"]), ("1", "true", "blob:none"))
        old = FakeServer(refs, main.pack_repository(server, [c2])[0], caps=["ls-refs", "fetch=shallow"])
        code, _, err = run(["clone", "http://example.invalid/app.git", self.path("full"), "--filter", "blob:none"], old)
        self.assertEqual(code, 0, err)
        self.assertIn("filtering not recognized by server", err)
        self.assertNotIn("filter blob:none", old.fetches()[0])
        self.assertEqual(run(["clone", "http://example.invalid/app.git", self.path("bad"), "--filter", "sparse:path=x"], old)[0], 2)
        good = main.pack_repository(server, [c2])[0]
        broken = FakeServer(refs, good[:-1] + bytes([good[-1] ^ 1]))
        code, _, err = run(["clone", "http://example.invalid/app.git", self.path("broken")], broken)
        self.assertEqual(code, 1)
        self.assertIn("pack checksum mismatch", err)
        self.assertFalse(os.path.exists(self.path("broken")))

    def test_fetch_negotiates_in_rounds_and_never_sends_ancestors_of_common_commits(self):
        server = main.Repository.init(self.path("server"))
        root = commit_file(server, "root\n", [], 1000)
        shared = commit_file(server, "shared\n", [root], 2000)
        newer = commit_file(server, "newer\n", [shared], 9000)
        pack = main.write_pack(main.plan_pack([(w.oid, w.type, server.read(w.oid)[1], w.path) for w in main.walk_objects(server, [newer], shallow={newer})]))[0]
        for advertise_shared, expected in ((False, [(16, False, False), (6, False, True)]), (True, [(16, True, False)])):
            client_dir = self.path(f"client-{advertise_shared}")
            client = main.Repository.init(client_dir)
            for oid in (root, shared):
                kind, data = server.read(oid)
                client.write(kind, data)
                client.write("tree", server.read(main.parse_commit(data)[0])[1])
                client.write("blob", server.read(main.parse_tree(server.read(main.parse_commit(data)[0])[1])[0][2])[1])
            tip = shared
            for i in range(20):
                tip = commit_file(client, f"local {i}\n", [tip], 3000 + i)
            client.write_ref("refs/heads/local", tip)
            client.write_ref("refs/remotes/origin/main", shared)
            with open(os.path.join(client.gitdir, "config"), "a") as handle:
                handle.write('[remote "origin"]\n\turl = http://example.invalid/app.git\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n')
            refs = [f"{newer} refs/heads/main"] + ([f"{shared} refs/heads/old"] if advertise_shared else [])
            fake = FakeServer(refs, pack, known={root, shared, newer})
            code, out, err = run(["fetch", client_dir, "--json"], fake)
            self.assertEqual(code, 0, err)
            summary = json.loads(out)
            self.assertEqual([(r["haves"], r["ready"], r["done"]) for r in summary["rounds"]], expected)
            self.assertEqual(["done" in args for args in fake.fetches()], [e[2] for e in expected])
            sent = [a[5:] for args in fake.fetches() for a in args if a.startswith("have ")]
            self.assertEqual(root in sent, not advertise_shared)
            self.assertEqual(summary["updated"]["refs/remotes/origin/main"], [shared, newer])
            self.assertTrue(main.Repository(client_dir).has(newer))


class CliTests(TempDirTest):
    def test_delta_command_and_input_errors(self):
        base, target = self.path("base.txt"), self.path("target.txt")
        with open(base, "w") as handle:
            handle.write("alpha\n" * 50)
        with open(target, "w") as handle:
            handle.write("alpha\n" * 50 + "beta\n")
        code, out, _ = run(["delta", base, target, "--json"])
        report = json.loads(out)
        self.assertEqual((code, report["base"], report["target"], report["copied"], report["inserted"]), (0, 300, 305, 300, 5))
        self.assertEqual(run(["delta", base, self.path("missing")])[0], 2)
        self.assertEqual(run(["clone", "ftp://example.invalid/x", self.path("x")])[0], 2)
        self.assertEqual(run(["fetch", self.tmp])[0], 2)

    def test_verify_pack_flags_a_pack_that_does_not_match_its_index(self):
        pack, records, checksum = main.write_pack(main.plan_pack(similar_blobs()))
        stem = self.path(f"pack-{checksum}")
        with open(stem + ".pack", "wb") as handle:
            handle.write(pack)
        self.assertEqual(run(["index-pack", stem + ".pack"])[1].strip(), checksum)
        code, out, _ = run(["verify-pack", "-v", stem + ".idx"])
        self.assertEqual(code, 0)
        self.assertIn("chain length = 1: 2 objects", out)
        os.chmod(stem + ".idx", 0o644)
        with open(stem + ".idx", "wb") as handle:
            handle.write(main.write_idx([(oid, crc ^ 1, off) for oid, crc, off in records], checksum))
        code, _, err = run(["verify-pack", stem + ".pack"])
        self.assertEqual(code, 1)
        self.assertIn("crc32", err)


def git_env(home):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.path.join(home, "gitconfig"), GIT_TERMINAL_PROMPT="0", NO_PROXY="127.0.0.1", no_proxy="127.0.0.1")
    return env


def git(env, *args, cwd=None, when=None):
    if when is not None:
        env = dict(env, GIT_AUTHOR_DATE=f"{when} +0000", GIT_COMMITTER_DATE=f"{when} +0000")
    result = subprocess.run([GIT, "-c", "user.name=CC Test", "-c", "user.email=test@example.invalid", "-c", "init.defaultBranch=main", *args], cwd=cwd, env=env, capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout


def make_history(env, path, commits=6):
    git(env, "init", "-q", path)
    os.makedirs(os.path.join(path, "src"), exist_ok=True)
    for i in range(1, commits + 1):
        with open(os.path.join(path, "numbers.txt"), "w") as handle:
            handle.write("".join(f"{n}\n" for n in range(i * 60)))
        with open(os.path.join(path, "src", f"mod{i}.py"), "w") as handle:
            handle.write(f"VALUE = {i}\n")
        git(env, "add", "-A", cwd=path)
        git(env, "commit", "-q", "-m", f"change {i}", cwd=path, when=1700000000 + 60 * i)
        if i == 2:
            git(env, "tag", "-a", "v1", "-m", "v1", cwd=path, when=1700000000 + 60 * i)
    git(env, "branch", "topic", "HEAD~1", cwd=path)


def object_list(env, path, *extra):
    return sorted(git(env, "rev-list", "--objects", "--all", *extra, cwd=path).splitlines())


@unittest.skipUnless(GIT, "git is not installed")
class RealPackTests(TempDirTest):
    def setUp(self):
        super().setUp()
        self.env = git_env(self.tmp)
        self.work = self.path("work")
        make_history(self.env, self.work)

    def check_with_git(self, pack, idx, name):
        directory = self.path(name)
        os.makedirs(directory)
        stem = os.path.join(directory, f"pack-{pack[-20:].hex()}")
        with open(stem + ".pack", "wb") as handle:
            handle.write(pack)
        printed = git(self.env, "index-pack", stem + ".pack").strip()
        self.assertEqual(printed, pack[-20:].hex())
        with open(stem + ".idx", "rb") as handle:
            self.assertEqual(handle.read(), idx, "minipack's .idx differs from git index-pack's")
        return git(self.env, "verify-pack", "-v", stem + ".idx")

    def test_git_accepts_minipack_packs_and_computes_the_same_idx(self):
        repo = main.Repository(self.work)
        for ofs in (True, False):
            pack, idx, plans = main.pack_repository(repo, main.pack_tips(repo, [], True), ofs_delta=ofs)
            report = self.check_with_git(pack, idx, f"check-{ofs}")
            self.assertIn("chain length = 1:", report)
            self.assertEqual(sum(1 for line in report.splitlines() if line[:40] in {p.oid for p in plans}), len(plans))
            self.assertGreater(sum(1 for p in plans if p.base), 0)
        reachable = {line.split()[0] for line in git(self.env, "rev-list", "--objects", "--all", cwd=self.work).splitlines()}
        self.assertEqual({p.oid for p in plans}, reachable)

    def test_minipack_reads_packs_git_writes_with_both_delta_kinds(self):
        for use_offset in ("true", "false"):
            git(self.env, "-c", f"repack.useDeltaBaseOffset={use_offset}", "repack", "-adfq", cwd=self.work)
            directory = os.path.join(self.work, ".git", "objects", "pack")
            pack_name = next(n for n in os.listdir(directory) if n.endswith(".pack"))
            with open(os.path.join(directory, pack_name), "rb") as handle:
                indexed = main.index_pack(handle.read())
            with open(os.path.join(directory, pack_name[:-5] + ".idx"), "rb") as handle:
                self.assertEqual(main.write_idx(indexed.records(), indexed.checksum), handle.read())
            kinds = {e.type_code for e in indexed.entries}
            self.assertIn(main.OBJ_OFS_DELTA if use_offset == "true" else main.OBJ_REF_DELTA, kinds)
            repo = main.Repository(self.work)
            listing = git(self.env, "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype) %(objectsize)", cwd=self.work)
            for line in listing.splitlines():
                oid, kind, size = line.split()
                got_kind, data = repo.read(oid)
                self.assertEqual((got_kind, len(data)), (kind, int(size)), oid)


@unittest.skipUnless(http_backend_available(), "git http-backend is not available")
class HttpBackendTests(TempDirTest):
    def setUp(self):
        super().setUp()
        self.env = git_env(self.tmp)
        make_history(self.env, self.path("work"))
        os.makedirs(self.path("srv"))
        git(self.env, "clone", "-q", "--bare", self.path("work"), self.path("srv", "app.git"))
        git(self.env, "config", "uploadpack.allowFilter", "true", cwd=self.path("srv", "app.git"))
        self.server = main.start_server(self.path("srv"))
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = main.server_url(self.server) + "/app.git"

    def test_full_shallow_and_blobless_clones_match_git_clone(self):
        for name, options in (("full", []), ("shallow", ["--depth", "1"]), ("blobless", ["--filter", "blob:none"])):
            mine, theirs = self.path(f"mp-{name}"), self.path(f"git-{name}")
            code, _, err = run(["clone", self.url, mine, *options])
            self.assertEqual(code, 0, err)
            git(self.env, "clone", "-q", "--no-checkout", *(["--depth", "1"] if name == "shallow" else ["--filter=blob:none"] if name == "blobless" else []), self.url, theirs)
            self.assertEqual(object_list(self.env, mine, "--missing=print"), object_list(self.env, theirs, "--missing=print"), name)
            git(self.env, "fsck", "--strict", "--no-progress", cwd=mine)
            self.assertEqual(main.Repository(mine).shallow(), main.Repository(theirs).shallow(), name)
        self.assertTrue(any(line.startswith("?") for line in object_list(self.env, self.path("mp-blobless"), "--missing=print")))

    def test_fetch_after_a_push_sends_haves_and_receives_only_new_objects(self):
        mine, theirs = self.path("mp"), self.path("git")
        self.assertEqual(run(["clone", self.url, mine])[0], 0)
        git(self.env, "clone", "-q", "--no-checkout", self.url, theirs)
        work = self.path("work")
        with open(os.path.join(work, "numbers.txt"), "a") as handle:
            handle.write("new line\n")
        git(self.env, "commit", "-qam", "change 7", cwd=work, when=1700001000)
        git(self.env, "push", "-q", self.path("srv", "app.git"), "main", cwd=work)
        code, out, err = run(["fetch", mine, "--json"])
        self.assertEqual(code, 0, err)
        summary = json.loads(out)
        self.assertTrue(any(a.startswith("have ") for r in summary["requests"] if r["command"] == "fetch" for a in r["arguments"]))
        self.assertEqual(summary["pack"]["objects"], 3)
        git(self.env, "fetch", "-q", cwd=theirs)
        self.assertEqual(object_list(self.env, mine), object_list(self.env, theirs))
        self.assertEqual(run(["fetch", mine])[1].strip(), "already up to date")

    def test_the_cgi_wrapper_accepts_chunked_request_bodies(self):
        host, port = self.server.server_address[:2]
        body = main.pkt_line("command=ls-refs\n") + main.DELIM + main.pkt_line("ref-prefix refs/heads/\n") + main.FLUSH
        connection = http.client.HTTPConnection(host, port, timeout=30)
        connection.request("POST", "/app.git/git-upload-pack", body=iter([body[:7], body[7:]]), encode_chunked=True,
                           headers={"Git-Protocol": "version=2", "Content-Type": "application/x-git-upload-pack-request"})
        response = connection.getresponse()
        names = sorted(p.text().split(" ")[1] for p in main.read_packets(response.read()) if p.kind == "data")
        connection.close()
        self.assertEqual((response.status, names), (200, ["refs/heads/main", "refs/heads/topic"]))


if __name__ == "__main__":
    unittest.main()
