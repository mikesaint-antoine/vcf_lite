"""Core tests on synthetic VCFs, checked against a brute-force reference.

Run:  python -m pytest -q
Tabix/CSI tests need ``bgzip``/``tabix``/``bcftools`` on PATH and are skipped
otherwise.
"""

import gzip
import random
import shutil
import struct
import subprocess
import zlib

import pytest

from vcflite.api import Api
from vcflite.vcf import VcfFile, is_nonvariant, record_end

HAVE_HTSLIB = all(shutil.which(t) for t in ("bgzip", "tabix", "bcftools"))

CONTIGS = [("chr1", 300_000), ("chr2", 200_000), ("chrX", 150_000), ("chrM", 16_569), ("chrUn_gl000220", 5_000)]


def make_records(seed=1):
    rnd = random.Random(seed)
    recs = []
    for name, length in CONTIGS:
        pos = rnd.randint(1, 50)
        while pos < length:
            ref = rnd.choice("ACGT")
            kind = rnd.random()
            if kind < 0.5:  # hom-ref site
                recs.append((name, pos, ".", ref, ".", "30", ".", "DP=3", "GT:DP", "0/0:3"))
                step = 1
            elif kind < 0.6:  # reference block
                end = pos + rnd.randint(5, 400)
                recs.append((name, pos, ".", ref, "<NON_REF>", ".", ".", f"END={end}", "GT:DP", "0/0:9"))
                step = end - pos + 1
            elif kind < 0.7:  # deletion
                dl = rnd.randint(2, 30)
                recs.append((name, pos, f"rs{pos}", ref * dl, ref, "50", "PASS", "DP=10", "GT:DP", "0/1:10"))
                step = rnd.randint(1, 60)
            else:
                alt = rnd.choice([b for b in "ACGT" if b != ref])
                gt = rnd.choice(["0/1", "1/1", "0|1"])
                recs.append((name, pos, f"rs{pos}", ref, alt, "50", "PASS", f"DP={rnd.randint(1, 99)}", "GT:DP", f"{gt}:7"))
                step = rnd.randint(1, 60)
            pos += step
    return recs


def header():
    lines = ["##fileformat=VCFv4.2", "##reference=GRCh38.fa"]
    lines += [f"##contig=<ID={n},length={l}>" for n, l in CONTIGS]
    lines += ['##INFO=<ID=DP,Number=1,Type=Integer,Description="Depth">',
              '##INFO=<ID=END,Number=1,Type=Integer,Description="End">',
              '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">',
              "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS1"]
    return "\n".join(lines) + "\n"


def bgzf_compress(data: bytes) -> bytes:
    out = bytearray()
    for i in range(0, len(data), 60000):
        chunk = data[i:i + 60000]
        c = zlib.compressobj(6, zlib.DEFLATED, -15)
        cdata = c.compress(chunk) + c.flush()
        bsize = 18 + len(cdata) + 8 - 1
        out += b"\x1f\x8b\x08\x04\x00\x00\x00\x00\x00\xff\x06\x00BC\x02\x00" + struct.pack("<H", bsize)
        out += cdata + struct.pack("<II", zlib.crc32(chunk) & 0xFFFFFFFF, len(chunk))
    out += bytes.fromhex("1f8b08040000000000ff0600424302001b0003000000000000000000")
    return bytes(out)


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    import os
    os.environ["VCFLITE_CACHE"] = str(tmp_path_factory.mktemp("cache"))
    d = tmp_path_factory.mktemp("vcf")
    recs = make_records()
    text = (header() + "".join("\t".join(map(str, r)) + "\n" for r in recs)).encode()
    files = {}
    (d / "a.vcf").write_bytes(text)
    files["text"] = d / "a.vcf"
    (d / "a.plain.vcf.gz").write_bytes(gzip.compress(text))
    files["gzip"] = d / "a.plain.vcf.gz"
    (d / "a.noidx.vcf.gz").write_bytes(bgzf_compress(text))
    files["bgzf-scan"] = d / "a.noidx.vcf.gz"
    if HAVE_HTSLIB:
        (d / "t.vcf.gz").write_bytes(bgzf_compress(text))
        subprocess.run(["tabix", "-p", "vcf", str(d / "t.vcf.gz")], check=True)
        files["tbi"] = d / "t.vcf.gz"
        (d / "c.vcf.gz").write_bytes(bgzf_compress(text))
        subprocess.run(["bcftools", "index", "-c", str(d / "c.vcf.gz")], check=True)
        files["csi"] = d / "c.vcf.gz"
    return recs, files


def expected_locate(recs, contig, pos):
    seen = False
    for r in recs:
        if r[0] == contig:
            seen = True
            end = record_end([str(x).encode() for x in r])
            if r[1] >= pos or end >= pos:
                return r
        elif seen:
            return r  # ran off the end of the contig
    return None


def open_indexed(path):
    v = VcfFile(str(path))
    if not v.index_ready:
        v.build_index()
    return v


KINDS = ["text", "gzip", "bgzf-scan", "tbi", "csi"]


@pytest.mark.parametrize("kind", KINDS)
def test_locate_matches_bruteforce(dataset, kind):
    recs, files = dataset
    if kind not in files:
        pytest.skip("htslib tools not available")
    v = open_indexed(files[kind])
    assert v.contigs[:3] == ["chr1", "chr2", "chrX"]
    rnd = random.Random(kind)
    queries = [(c, 1) for c, _ in CONTIGS] + [(c, l + 10) for c, l in CONTIGS]
    queries += [(rnd.choice(CONTIGS)[0], rnd.randint(1, 300_000)) for _ in range(150)]
    for c, p in queries:
        exp = expected_locate(recs, c, p)
        off = v.locate(c, p)
        if exp is None:
            assert off is None, (c, p)
            continue
        line = next(v.source.iter_lines(off))[1].decode()
        got = line.split("\t")[:2]
        assert got == [exp[0], str(exp[1])], (kind, c, p)


@pytest.mark.parametrize("kind", KINDS)
def test_paging_forward_and_back(dataset, kind):
    recs, files = dataset
    if kind not in files:
        pytest.skip("htslib tools not available")
    v = open_indexed(files[kind])
    # forward through the whole file
    all_rows, off = [], v.data_start
    while off is not None:
        rows, off, eof = v.read_rows(off, 997)
        all_rows += rows
    assert [(r["c"][0], int(r["c"][1])) for r in all_rows] == [(r[0], r[1]) for r in recs]
    # backwards from the end reproduces the same sequence
    back, top, at_start = [], None, False
    top = all_rows[-1]["o"]
    back = [all_rows[-1]]
    while not at_start:
        rows, at_start = v.read_before(top, 1000)
        if not rows:
            break
        back = rows + back
        top = rows[0]["o"]
    assert [r["o"] for r in back] == [r["o"] for r in all_rows]


@pytest.mark.parametrize("kind", ["text", "bgzf-scan", "tbi"])
def test_hide_ref(dataset, kind):
    recs, files = dataset
    if kind not in files:
        pytest.skip("htslib tools not available")
    v = open_indexed(files[kind])
    rows, off = [], v.data_start
    while off is not None:
        r, off, _ = v.read_rows(off, 500, hide_ref=True)
        rows += r
    exp = [r for r in recs if not is_nonvariant([str(x).encode() for x in r])]
    assert [(r["c"][0], int(r["c"][1])) for r in rows] == [(r[0], r[1]) for r in exp]
    assert not any(r["r"] for r in rows)


@pytest.mark.parametrize("kind", ["text", "gzip", "bgzf-scan"])
def test_text_search(dataset, kind):
    recs, files = dataset
    v = open_indexed(files[kind])
    target = [r for r in recs if r[2].startswith("rs")][len(recs) // 5]
    off = v.search_text(target[2])
    line = next(v.source.iter_lines(off))[1].decode().split("\t")
    assert line[2] == target[2]
    # whole-token match: "rs1" must not match "rs12"
    assert v.search_text("rs999999999") is None
    # search continues after a hit
    first = v.search_text("<NON_REF>")
    nxt = v.search_text("<NON_REF>", start=v.next_line_offset(first))
    assert nxt is not None and nxt > first
    # restricted to the ID column, INFO/ALT text doesn't match
    assert v.search_text("<NON_REF>", column=2) is None
    assert v.search_text("DP", column=2) is None


def test_nonvariant():
    f = lambda s: is_nonvariant([x.encode() for x in s.split()])
    assert f("1 5 . A . . . . GT 0/0")
    assert f("1 5 . A G . . . GT:DP 0|0:3 ./.:1")
    assert not f("1 5 . A G . . . GT:DP 0/1:3")
    assert not f("1 5 . A G . . .")  # sites-only with an ALT
    assert f("1 5 . A <NON_REF> . . END=9 GT 0/0")


def test_api_goto_and_build(dataset):
    recs, files = dataset
    a = Api()
    r = a.open(str(files["text"]))
    assert r["page"]["rows"][0]["c"][0] == "chr1"
    if a._index_task:
        a._index_task._thread.join()
    g = a.goto("1", "150,000")  # alias without "chr", commas
    assert g["page"]["rows"][0]["c"][0] == "chr1"
    assert int(g["page"]["rows"][0]["c"][1]) >= 149_000
    assert a.goto("x", "1k")["page"]["rows"][0]["c"][0] == "chrX"
    assert a.goto("MT")["page"]["rows"][0]["c"][0] == "chrM"
    assert a.goto("chr9", "100")["field"] == "chrom"
    assert a.goto("chr1", "abc")["field"] == "pos"
    # rsID search looks only in the ID column; bare numbers mean rsN
    target = [r for r in recs if r[2].startswith("rs")][40]
    assert a.find_id(target[2][2:]) == {"searching": True, "text": target[2]}
    a._task._thread.join()
    off = a._task.result["offset"]
    assert a.row(off)["c"][2] == target[2]
    import time
    for _ in range(50):
        if a.status().get("build"):
            break
        time.sleep(0.1)
    assert a.status()["build"]["build"] == "GRCh38"
    assert a.status()["kind"]["kind"] == "gvcf"


@pytest.mark.parametrize("kind", ["text", "bgzf-scan", "tbi"])
def test_coverage_estimate(dataset, kind):
    from vcflite import detect
    recs, files = dataset
    if kind not in files:
        pytest.skip("htslib tools not available")
    v = open_indexed(files[kind])
    # exact answer from the synthetic records (autosomes = chr1, chr2)
    auto = [r for r in recs if r[0] in ("chr1", "chr2")]
    dp = [int(r[7].split("DP=")[1]) if "DP=" in r[7] else 9 for r in auto]
    span = [record_end([str(x).encode() for x in r]) - r[1] + 1 for r in auto]
    covered = sum(d * s for d, s in zip(dp, span)) / sum(span)
    breadth = sum(span) / (300_000 + 200_000)
    cov = detect.estimate_coverage(v, "GRCh38", detect.classify(v))
    assert "of genome covered" in cov["why"]
    got_breadth = int(cov["why"].split("; ")[1].split("%")[0]) / 100
    got_covered = float(cov["why"].split("covered, ")[1].split("×")[0])
    assert abs(got_breadth - breadth) < 0.15
    assert abs(got_covered - covered) / covered < 0.25


def test_phasing(dataset):
    from vcflite import detect
    recs, files = dataset
    v = open_indexed(files["text"])
    # synthetic hets are a mix of "0/1" and "0|1"
    r = detect.phasing(v)
    assert r["label"] == "mixed"
    hets = [r[9].split(":")[0] for r in recs if r[9].split(":")[0] in ("0/1", "0|1")]
    frac = sum(g == "0|1" for g in hets) / len(hets)
    assert abs(int(r["why"].split("%")[0]) / 100 - frac) < 0.1


def test_multisample(tmp_path):
    """Three samples: hide-hom-ref must look at every sample (regression:
    only the first two were checked), and a picked sample is judged alone."""
    rows = [
        ("1", 100, "0/0", "0/0", "0/1"),   # only the 3rd sample carries it
        ("1", 200, "0/1", "0/0", "0/0"),
        ("1", 300, "0/0", "0/0", "0/0"),   # nobody
        ("1", 400, "./.", "1/1", "0/0"),
    ]
    hdr = ("##fileformat=VCFv4.2\n##contig=<ID=1,length=1000>\n"
           '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
           '##FORMAT=<ID=AD,Number=R,Type=Integer,Description="Depths">\n'
           "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tA\tB\tC\n")
    body = "".join(f"{c}\t{p}\t.\tA\tG\t50\tPASS\t.\tGT:AD\t{a}:5,5\t{b}:6,0\t{d}:7,1\n" for c, p, a, b, d in rows)
    f = tmp_path / "trio.vcf"
    f.write_text(hdr + body)
    v = open_indexed(f)
    pos = lambda rs: [int(r["c"][1]) for r in rs]

    all_rows, _, _ = v.read_rows(v.data_start, 10)
    assert pos(all_rows) == [100, 200, 300, 400]
    assert [r["r"] for r in all_rows] == [False, False, True, False]
    assert len(all_rows[0]["c"]) == 12  # all three samples

    shown, _, _ = v.read_rows(v.data_start, 10, hide_ref=True)
    assert pos(shown) == [100, 200, 400]

    # Picking sample C: its column comes back as c[9]; hom-ref judged for C only.
    c_rows, _, _ = v.read_rows(v.data_start, 10, sample=2)
    assert [r["c"][9].split(":")[0] for r in c_rows] == ["0/1", "0/0", "0/0", "0/0"]
    assert len(c_rows[0]["c"]) == 10
    c_shown, _, _ = v.read_rows(v.data_start, 10, hide_ref=True, sample=2)
    assert pos(c_shown) == [100]
    b_shown, _, _ = v.read_rows(v.data_start, 10, hide_ref=True, sample=1)
    assert pos(b_shown) == [400]
    # backwards paging honours the pick too
    back, _ = v.read_before(all_rows[-1]["o"], 10, hide_ref=True, sample=2)
    assert pos(back) == [100]
