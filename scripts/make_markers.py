"""Build vcflite/data/build_markers.tsv - a small panel of common SNPs used to
guess the genome build of VCFs whose header doesn't say.

Markers are SNPs where the *reference* allele is the rare one (global MAF of
the REF allele < 10%), so almost everyone is ALT and they show up even in
variants-only files.  Each has a different position in GRCh37 and GRCh38.

Usage:  python scripts/make_markers.py path/to/grch37_file_with_rsids.vcf.gz
(needs bcftools on PATH and internet access to rest.ensembl.org)
"""

import json
import random
import subprocess
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

PER_CHROM = 10
CHROMS = [str(i) for i in range(1, 23)] + ["X"]
OUT = Path(__file__).resolve().parent.parent / "vcflite" / "data" / "build_markers.tsv"


def ensembl(server, ids):
    req = urllib.request.Request(
        f"https://{server}/variation/homo_sapiens",
        data=json.dumps({"ids": ids}).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception as e:  # rate limits etc.
            print("retry", e, file=sys.stderr)
            time.sleep(2 + attempt * 3)
    raise SystemExit("Ensembl request failed")


def mapping(rec):
    maps = [m for m in rec.get("mappings", [])
            if m.get("coord_system") == "chromosome" and m["seq_region_name"] in CHROMS]
    if len(maps) != 1 or maps[0]["start"] != maps[0]["end"]:
        return None
    return maps[0]


def main(vcf):
    random.seed(7)
    cands = []
    for c in CHROMS:
        out = subprocess.run(
            ["bcftools", "view", "-H", "-v", "snps", "-i", 'GT="AA" && ID!="."', vcf, c],
            capture_output=True, text=True).stdout.splitlines()
        rs = [l.split("\t")[2] for l in out if l.split("\t")[2].startswith("rs")]
        # spread evenly along the chromosome
        step = max(1, len(rs) // (PER_CHROM * 6))
        cands += rs[::step][:PER_CHROM * 6]
        print(c, len(rs), file=sys.stderr)

    by_chrom = defaultdict(list)
    for i in range(0, len(cands), 200):
        ids = cands[i:i + 200]
        g37 = ensembl("grch37.rest.ensembl.org", ids)
        g38 = ensembl("rest.ensembl.org", ids)
        for rid in ids:
            a, b = g37.get(rid), g38.get(rid)
            if not a or not b:
                continue
            m37, m38 = mapping(a), mapping(b)
            if not m37 or not m38 or m37["seq_region_name"] != m38["seq_region_name"]:
                continue
            if m37["start"] == m38["start"]:
                continue
            al37, al38 = m37["allele_string"].split("/"), m38["allele_string"].split("/")
            if al37[0] != al38[0] or len(al37[0]) != 1:
                continue
            maf, minor = b.get("MAF"), b.get("minor_allele")
            if maf is None or minor != al38[0] or float(maf) > 0.1:
                continue  # REF must be the rare allele
            by_chrom[m37["seq_region_name"]].append(
                (rid, m37["seq_region_name"], m37["start"], m38["start"], al37[0]))
        time.sleep(0.5)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w") as f:
        f.write("#rsid\tchrom\tpos_grch37\tpos_grch38\tref\n")
        n = 0
        for c in CHROMS:
            for row in by_chrom[c][:PER_CHROM]:
                f.write("\t".join(map(str, row)) + "\n")
                n += 1
    print(f"wrote {n} markers to {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1])
