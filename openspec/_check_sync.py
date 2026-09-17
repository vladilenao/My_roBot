import re, os, glob
CH = "openspec/changes/add-trade-management-profiles/specs"
MAIN = "openspec/specs"

def norm(s):
    return re.sub(r"\s+", " ", s).strip()

def parse_reqs(path):
    txt = open(path, encoding="utf-8").read()
    out = {}
    cur = None
    buf = []
    def flush():
        if cur is not None:
            out[cur] = "\n".join(buf).strip()
    for line in txt.splitlines():
        m = re.match(r"### Requirement:\s*(.+)$", line)
        if m:
            flush()
            cur = m.group(1).strip()
            buf = [line]
        else:
            if cur is not None:
                buf.append(line)
    flush()
    return out

def delta_ops(path):
    txt = open(path, encoding="utf-8").read()
    removed, added, modified = [], [], []
    for sec in re.split(r"(?m)^## ", txt)[1:]:
        nl = sec.split("\n", 1)
        hdr = nl[0].strip().lower()
        body = nl[1] if len(nl) > 1 else ""
        if hdr.startswith("removed"):
            cap = removed
        elif hdr.startswith("add"):
            cap = added
        elif hdr.startswith("modif"):
            cap = modified
        else:
            continue
        for b in re.split(r"(?m)^### Requirement:", body)[1:]:
            first = b.split("\n", 1)
            nm = first[0].strip()
            block = "### Requirement: " + nm + "\n" + (first[1] if len(first) > 1 else "")
            cap.append((nm, block))
    return removed, added, modified

def delta_req_names(path):
    txt = open(path, encoding="utf-8").read()
    out = {"removed": set(), "added": set(), "modified": set()}
    for sec in re.split(r"(?m)^## ", txt)[1:]:
        nl = sec.split("\n", 1)
        hdr = nl[0].strip().lower()
        body = nl[1] if len(nl) > 1 else ""
        if hdr.startswith("removed"):
            key = "removed"
        elif hdr.startswith("add"):
            key = "added"
        elif hdr.startswith("modif"):
            key = "modified"
        else:
            continue
        for b in re.split(r"(?m)^### Requirement:", body)[1:]:
            nm = b.split("\n", 1)[0].strip()
            out[key].add(nm)
    return out

caps = []
for dp in sorted(glob.glob(f"{CH}/*/spec.md")):
    rel = os.path.relpath(dp, CH)
    cap = os.path.dirname(rel)
    # find main path
    if os.path.exists(os.path.join(MAIN, cap, "spec.md")):
        mp = os.path.join(MAIN, cap, "spec.md")
    else:
        # nested search
        cand = glob.glob(f"{MAIN}/**/{cap}/spec.md", recursive=True)
        mp = cand[0] if cand else None
    caps.append((cap, dp, mp))

for cap, dp, mp in caps:
    if mp is None:
        rem, add, mod = delta_ops(dp)
        print(f"[NEW] {cap}: create main from delta ({len(add)} added)")
        continue
    mreqs = parse_reqs(mp)
    dnames = delta_req_names(dp)
    issues = []
    for nm in dnames["removed"]:
        if nm in mreqs:
            issues.append(f"REMOVED '{nm}' still in main")
    for nm in dnames["added"]:
        if nm not in mreqs:
            issues.append(f"ADDED '{nm}' missing in main")
    rem, add, mod = delta_ops(dp)
    modmap = {nm: blk for nm, blk in mod}
    for nm, blk in mod:
        if nm not in mreqs:
            issues.append(f"MODIFIED '{nm}' missing in main")
        elif norm(mreqs[nm]) != norm(blk):
            issues.append(f"MODIFIED '{nm}' main differs from delta")
    if issues:
        print(f"[NEED] {cap}: " + "; ".join(issues))
    else:
        print(f"[OK  ] {cap}")
