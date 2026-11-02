import re, sys, pathlib
LANGS = [("en","English"),("hi","हिन्दी"),("mr","मराठी"),("gu","ગુજરાતી"),("de","Deutsch"),("es","Español"),("pt","Português"),("zh","中文"),("ja","日本語")]
for p in sorted(pathlib.Path("i18n").glob("README.*.md")):
    cur = p.name.split(".")[1]
    lines = []
    for i,(code,name) in enumerate(LANGS):
        sep = " ·" if i < len(LANGS)-1 else ""
        if code == cur:
            item = f"<strong>{name}</strong>"
        else:
            href = "../README.md" if code == "en" else f"./README.{code}.md"
            item = f'<a href="{href}">{name}</a>'
        lines.append(f"    {item}{sep}")
    block = "  <p>\n" + "\n".join(lines) + "\n  </p>"
    text = p.read_text()
    pat = re.compile(r"  <p>\n    (?:<a href=\"\.\./README\.md\">English</a>|<strong>English</strong>).*?\n  </p>", re.S)
    new, n = pat.subn(block, text, count=1)
    assert n == 1, p
    p.write_text(new)
    note = re.search(r"</p>\n  <p>(<sub>|<em>)", new[new.index(block):new.index(block)+len(block)+20])
    print(p.name, "switcher ok", "note:", bool(note))
