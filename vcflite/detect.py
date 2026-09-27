"""Figure out what kind of VCF we're looking at: genome build and content."""

from __future__ import annotations

import bisect
import re
from importlib import resources

from .vcf import REF_ONLY_ALTS, VcfFile, is_nonvariant, record_end

# Primary-assembly chromosome lengths (Ensembl / UCSC).
LENGTHS = {
    "GRCh37": {"1": 249250621, "2": 243199373, "3": 198022430, "4": 191154276, "5": 180915260,
               "6": 171115067, "7": 159138663, "8": 146364022, "9": 141213431, "10": 135534747,
               "11": 135006516, "12": 133851895, "13": 115169878, "14": 107349540, "15": 102531392,
               "16": 90354753, "17": 81195210, "18": 78077248, "19": 59128983, "20": 63025520,
               "21": 48129895, "22": 51304566, "X": 155270560, "Y": 59373566},
    "GRCh38": {"1": 248956422, "2": 242193529, "3": 198295559, "4": 190214555, "5": 181538259,
               "6": 170805979, "7": 159345973, "8": 145138636, "9": 138394717, "10": 133797422,
               "11": 135086622, "12": 133275309, "13": 114364328, "14": 107043718, "15": 101991189,
               "16": 90338345, "17": 83257441, "18": 80373285, "19": 58617616, "20": 64444167,
               "21": 46709983, "22": 50818468, "X": 156040895, "Y": 57227415},
    "T2T-CHM13": {"1": 248387328, "2": 242696752, "3": 201105948, "X": 154259566},
    "NCBI36": {"1": 247249719, "2": 242951149, "3": 199501827, "X": 154913754},
}

_B = r"(?<![A-Za-z0-9])"
_E = r"(?![A-Za-z0-9])"
KEYWORDS = [
    ("T2T-CHM13", re.compile(_B + r"(chm13|t2t)", re.I)),
    ("GRCh38", re.compile(_B + r"(grch38|hg38|hs38(dh)?|b38|assembly38|GCA_000001405\.15|GCF_000001405\.(2[6-9]|[34]\d))" + _E, re.I)),
    ("GRCh37", re.compile(_B + r"(grch37|hg19|hs37d5|hs37|b37|human_g1k_v37|g1k_v37|GCA_000001405\.1|GCF_000001405\.(1[3-9]|2[0-5]))" + _E, re.I)),
    ("NCBI36", re.compile(_B + r"(ncbi36|hg18|b36)" + _E, re.I)),
]


def _bare(name: str) -> str:
    n = name[3:] if name.lower().startswith("chr") else name
    return {"M": "MT"}.get(n.upper(), n.upper())


def _keyword_build(text: str):
    found = {b for b, rx in KEYWORDS if rx.search(text)}
    return found.pop() if len(found) == 1 else None


def _load_markers():
    txt = resources.files("vcflite").joinpath("data/build_markers.tsv").read_text()
    out = []
    for line in txt.splitlines():
        if line and not line.startswith("#"):
            rid, c, p37, p38, ref = line.split("\t")
            out.append((rid, c, int(p37), int(p38), ref))
    return out


def build_from_header(v: VcfFile):
    # 1. Contig lengths are the strongest signal.
    votes = {}
    for c in v.header_contigs:
        try:
            length = int(c.get("length", ""))
        except ValueError:
            continue
        key = _bare(c.get("ID", ""))
        for build, table in LENGTHS.items():
            if table.get(key) == length:
                votes[build] = votes.get(build, 0) + 1
    if votes:
        best = max(votes, key=votes.get)
        if all(n < votes[best] for b, n in votes.items() if b != best):
            return best, "contig lengths in header"

    # 2. The reference / assembly declared in the header.
    decl = [l for l in v.meta_lines if l.startswith(("##reference", "##assembly"))]
    decl += [c.get("assembly", "") for c in v.header_contigs[:5]]
    b = _keyword_build("\n".join(decl))
    if b:
        return b, "reference named in header"
    return None, None


def build_from_markers(v: VcfFile):
    """Look up known SNPs at their GRCh37 and GRCh38 positions."""
    if v.index is None:
        return None, None
    score = {"GRCh37": [0, 0], "GRCh38": [0, 0]}  # [ref matches, ref mismatches]
    markers = _load_markers()
    for i, (rid, c, p37, p38, ref) in enumerate(markers):
        contig = v.resolve_contig(c)
        if contig is None:
            continue
        for build, p in (("GRCh37", p37), ("GRCh38", p38)):
            for cols in v.records_at(contig, p):
                if cols[2].decode() == rid or cols[3].decode().upper() == ref:
                    score[build][0] += 1
                else:
                    score[build][1] += 1
                break
        if i >= 60:
            s37 = score["GRCh37"][0] - 3 * score["GRCh37"][1]
            s38 = score["GRCh38"][0] - 3 * score["GRCh38"][1]
            if abs(s37 - s38) >= 25:
                break
    s = {b: m - 3 * mm for b, (m, mm) in score.items()}
    best, other = ("GRCh37", "GRCh38") if s["GRCh37"] >= s["GRCh38"] else ("GRCh38", "GRCh37")
    if score[best][0] >= 4 and s[best] >= 4 and s[best] >= 3 * max(s[other], 0) + 3:
        return best, f"matched {score[best][0]} known SNP positions"
    return None, None


def detect_build(v: VcfFile):
    b, why = build_from_header(v)
    if b:
        return {"build": b, "why": why}
    b, why = build_from_markers(v)
    if b:
        return {"build": b, "why": why}
    b = _keyword_build("\n".join(v.meta_lines))
    if b:
        return {"build": b, "why": "mentioned in header"}
    b = _keyword_build(v.name)
    if b:
        return {"build": b, "why": "file name"}
    return {"build": None, "why": "no build information found"}


def classify(v: VcfFile, n=5000):
    """What's in the file: variants only, hom-ref sites too, or a gVCF."""
    if v.data_start is None:
        return {"kind": "empty", "label": "No records"}
    total = nonvar = refblocks = refonly = 0
    has_gt = False
    for off, line in v.source.iter_lines(v.data_start):
        if not line or line[0] == 35:
            continue
        cols = line.split(b"\t")
        if len(cols) < 8:
            continue
        total += 1
        if len(cols) > 9 and cols[8][:2] == b"GT":
            has_gt = True
        if cols[4] in REF_ONLY_ALTS:
            refonly += 1
        if is_nonvariant(cols):
            nonvar += 1
            if b"END=" in cols[7] and (cols[4] in REF_ONLY_ALTS or b"<NON_REF>" in cols[4] or b"<*>" in cols[4]):
                refblocks += 1
        if total >= n:
            break
    if total == 0:
        return {"kind": "empty", "label": "No records"}
    if refblocks:
        return {"kind": "gvcf", "label": "gVCF", "allSites": True}
    if nonvar / total >= 0.02:
        return {"kind": "homref", "label": "Includes hom-ref", "allSites": refonly / total >= 0.2, "tip":
                "Some records have no ALT allele in any sample (reference calls)."}
    if not v.samples:
        return {"kind": "sites", "label": "Sites only"}
    return {"kind": "variants", "label": "Variants only"}


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------

_INFO_DP = re.compile(rb"(?:^|;)DP=(\d+)")
_IMPUTED_HINT = re.compile(r"glimpse|impute|minimac|beagle|eagle|shapeit", re.I)


def _fmt_depth(x: float) -> str:
    if x < 1:
        return f"{x:.2f}×"
    return f"{x:.1f}×" if x < 10 else f"{x:.0f}×"


def _sample_contigs(v: VcfFile, build):
    """(contig, length) for the autosomes, with lengths from the header or
    the detected build."""
    header_len = {}
    for c in v.header_contigs:
        try:
            header_len[c.get("ID")] = int(c.get("length", ""))
        except ValueError:
            pass
    table = LENGTHS.get(build or "", {})
    out = []
    for n in range(1, 23):
        c = v.resolve_contig(str(n))
        if c is None:
            continue
        length = header_len.get(c) or table.get(str(n))
        if length:
            out.append((c, length))
    return out


def estimate_coverage(v: VcfFile, build=None, kind=None, points=600):
    """Estimate sequencing depth from DP fields.

    All-sites files and gVCFs list every covered base, so combining sampled
    DP with the index's exact record counts gives genome-wide mean depth and
    breadth.  Variants-only files only tell us depth at variant sites.
    """
    if v.index is None:
        return None
    contigs = _sample_contigs(v, build)
    if not contigs:
        return {"label": "unknown", "why": "couldn't find autosomes with known lengths"}
    total_len = sum(l for _, l in contigs)

    # Sample uniformly over *records*: evenly spaced blocks of the file, every
    # record in each.  (Sampling genome positions, or index entry points,
    # over-represents sparse low-depth regions.)
    autosomes = {c for c, _ in contigs}
    spans = []           # every sampled record (for breadth)
    dps, dp_spans = [], []  # records that carry a depth
    used = set()
    for lines in v.source.sample_lines(points):
        for line in lines:
            cols = line.split(b"\t")
            if len(cols) < 8 or cols[0].decode("utf-8", "replace") not in autosomes:
                continue
            try:
                span = max(1, record_end(cols) - int(cols[1]) + 1)
            except ValueError:
                continue
            spans.append(span)
            dp = None
            if len(cols) > 9:
                keys = cols[8].split(b":")
                if b"DP" in keys:
                    k = keys.index(b"DP")
                    vals = []
                    for smp in cols[9:]:
                        f = smp.split(b":")
                        if k < len(f) and f[k].isdigit():
                            vals.append(int(f[k]))
                    if vals:
                        dp = sum(vals) / len(vals)
                        used.add("FORMAT")
            if dp is None:
                m = _INFO_DP.search(cols[7])
                if m:
                    dp = int(m.group(1))
                    used.add("INFO")
            if dp is not None:
                dps.append(dp)
                dp_spans.append(span)

    if len(dps) < 20:
        imputed = ("DS" in v.format_defs or "GP" in v.format_defs
                   or any(_IMPUTED_HINT.search(l) for l in v.meta_lines))
        if imputed:
            return {"label": "n/a", "why": "imputed genotypes have no read depth"}
        if not spans:
            return {"label": "unknown", "why": "no records on the autosomes"}
        return {"label": "unknown", "why": "no read depth (DP) in this file"}

    src = "FORMAT" if "FORMAT" in used else "INFO"
    desc = (v.format_defs if src == "FORMAT" else v.info_defs).get("DP", {}).get("Description", "")
    tip = f"From {src}/DP" + (f": {desc}" if desc else "")
    all_sites = bool(kind and kind.get("allSites"))
    if all_sites:
        covered_depth = sum(d * s for d, s in zip(dps, dp_spans)) / sum(dp_spans)
        counts = [v.index.record_count(c) for c, _ in contigs]
        if all(n is not None for n in counts):
            mean_span = sum(spans) / len(spans)
            breadth = min(1.0, sum(counts) * mean_span / total_len)
            return {"label": _fmt_depth(covered_depth * breadth),
                    "why": f"mean depth; {breadth:.0%} of genome covered, {_fmt_depth(covered_depth)} where covered",
                    "tip": tip}
        return {"label": _fmt_depth(covered_depth), "why": "mean depth where covered", "tip": tip}
    med = sorted(dps)[len(dps) // 2]
    return {"label": "~" + _fmt_depth(med), "why": "median depth at variant sites", "tip": tip}


# ---------------------------------------------------------------------------
# Phasing
# ---------------------------------------------------------------------------

def phasing(v: VcfFile, points=200):
    """Are genotypes phased ("0|1") or not ("0/1")?

    Judged on heterozygous genotypes, since homozygous ones carry no phase
    and callers write them either way.  Sampled evenly through the file.
    """
    if not v.samples:
        return {"label": "n/a", "why": "no samples"}
    phased = unphased = 0
    any_gt = False
    for lines in v.source.sample_lines(points):
        for line in lines:
            cols = line.split(b"\t")
            if len(cols) < 10 or cols[8][:2] != b"GT" or line[:1] == b"#":
                continue
            any_gt = True
            for smp in cols[9:]:
                gt = smp.split(b":", 1)[0]
                if len(gt) < 3:
                    continue  # haploid or missing
                alleles = gt.replace(b"|", b"/").split(b"/")
                if b"." in alleles or len(set(alleles)) < 2:
                    continue  # missing or homozygous
                if b"|" in gt:
                    phased += 1
                else:
                    unphased += 1
    if not any_gt:
        return {"label": "n/a", "why": "no genotypes (GT) in this file"}
    n = phased + unphased
    if n == 0:
        return {"label": "n/a", "why": "no heterozygous genotypes found"}
    if unphased == 0:
        return {"label": "true", "why": "all heterozygous genotypes phased (|)"}
    if phased == 0:
        return {"label": "false", "why": "genotypes unphased (/)"}
    return {"label": "mixed", "why": f"{phased / n:.0%} of heterozygous genotypes phased"}
