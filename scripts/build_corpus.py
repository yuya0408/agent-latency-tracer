"""Build the document corpus for retrieve_documents: a few public rule
books and laws, cut into one chunk per article (docs/DESIGN.md §6, 7).

  python scripts/build_corpus.py

Downloads go to out/corpus_src/ (not committed) and are reused when present;
the chunks are written to data/corpus/chunks.jsonl.

Rules for the cut, all chosen so the result reads like a company's own rule
book rather than a reference text:
- One chunk per article, no overlap. Articles are self-contained units, and
  even the longest one is far below Titan V2's input limit.
- Main provisions only: supplementary provisions (附則) are amendment history
  and transition rules, which a company rule book doesn't carry.
- Where a model rule book offers alternatives (〔例２〕, 《…例》), only the first
  is kept: a company adopts one of them.
- The MHLW commentary around the articles is left out; blanks such as
  "　　　　株式会社" are kept as they are.
- Each chunk starts with the document title, article and caption, because an
  article alone ("前条の規定にかかわらず…") often doesn't say what it is about.
"""
from __future__ import annotations
import html, json, re, sys, unicodedata, urllib.request, zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT / "out" / "corpus_src"
OUT_PATH = ROOT / "data" / "corpus" / "chunks.jsonl"

EGOV_API = "https://laws.e-gov.go.jp/api/2/law_data/{law_id}?response_format=json"
EGOV_PAGE = "https://laws.e-gov.go.jp/law/{law_id}"


@dataclass(frozen=True)
class Source:
    doc_id: str
    title: str
    kind: str  # "egov", "mhlw_model" or "mhlw_ikuji"
    url: str  # what is downloaded
    page: str  # what the chunk cites


SOURCES = (
    Source("model-work-rules", "モデル就業規則", "mhlw_model",
           "https://www.mhlw.go.jp/content/001620506.docx",
           "https://www.mhlw.go.jp/content/001620506.docx"),
    Source("childcare-leave-rules", "育児・介護休業等に関する規則", "mhlw_ikuji",
           "https://www.mhlw.go.jp/content/11909000/001676343.docx",
           "https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/000103533.html"),
    Source("travel-expenses-act", "国家公務員等の旅費に関する法律", "egov",
           EGOV_API.format(law_id="325AC0000000114"), EGOV_PAGE.format(law_id="325AC0000000114")),
    Source("labor-standards-act", "労働基準法", "egov",
           EGOV_API.format(law_id="322AC0000000049"), EGOV_PAGE.format(law_id="322AC0000000049")),
)

# Titan Text Embeddings V2 takes up to 8,192 tokens. One Japanese character is
# assumed to be at most one token, so a chunk under this many characters fits.
MAX_CHARS = 8_000


@dataclass(frozen=True)
class Chunk:
    id: str
    doc_id: str
    doc_title: str
    article: str  # laws as e-Gov writes them ("第七条"), the MHLW rules in ASCII digits ("第19条")
    caption: str
    version: str  # the revision the text is taken from
    source: str
    text: str  # header line + the article's text; this is what is embedded and returned


def make_chunk(src: Source, version: str, seq: int, article: str, caption: str, body: str) -> Chunk:
    header = f"{src.title} {article}" + (f"（{caption}）" if caption else "")
    return Chunk(f"{src.doc_id}-{seq:03d}", src.doc_id, src.title, article, caption, version, src.page,
                 f"{header}\n{body.strip()}")


# --- e-Gov (laws) -----------------------------------------------------------

def _strings(node) -> str:
    if isinstance(node, str):
        return node
    return "".join(_strings(c) for c in node.get("children", []))


def _article_body(article: dict) -> str:
    """Paragraphs and items on their own lines, numbered as in the law."""
    lines: list[str] = []

    def walk(n) -> None:
        if isinstance(n, str):
            return
        tag = n.get("tag")
        if tag == "Paragraph":
            num = next((_strings(c) for c in n["children"] if isinstance(c, dict) and c["tag"] == "ParagraphNum"), "")
            sentence = "".join(_strings(c) for c in n["children"] if isinstance(c, dict) and c["tag"] == "ParagraphSentence")
            lines.append(f"{num}　{sentence}" if num else sentence)
            for c in n["children"]:
                if isinstance(c, dict) and c["tag"] not in ("ParagraphNum", "ParagraphSentence"):
                    walk(c)
        elif tag in ("Item",) or tag.startswith("Subitem"):
            title = next((_strings(c) for c in n["children"] if isinstance(c, dict) and c["tag"].endswith("Title")), "")
            sentence = "".join(_strings(c) for c in n["children"] if isinstance(c, dict) and c["tag"].endswith("Sentence"))
            lines.append(f"{title}　{sentence}")
            for c in n["children"]:
                if isinstance(c, dict) and not (c["tag"].endswith("Title") or c["tag"].endswith("Sentence")):
                    walk(c)
        else:
            for c in n.get("children", []):
                walk(c)

    walk(article)
    return "\n".join(lines)


def egov_articles(law: dict) -> list[tuple[str, str, str]]:
    """(article, caption, body) for each article of the main provision."""
    out: list[tuple[str, str, str]] = []

    def walk(n) -> None:
        if isinstance(n, str):
            return
        if n.get("tag") == "Article":
            caption = next((_strings(c) for c in n["children"] if isinstance(c, dict) and c["tag"] == "ArticleCaption"), "")
            title = next((_strings(c) for c in n["children"] if isinstance(c, dict) and c["tag"] == "ArticleTitle"), "")
            body = _article_body(n)
            if body.strip() != "削除":  # repealed articles keep their number with this one word
                out.append((title, caption.strip("（）()"), body))
            return
        if n.get("tag") == "SupplProvision":
            return
        for c in n.get("children", []):
            walk(c)

    walk(law["law_full_text"])
    return out


# --- MHLW Word files --------------------------------------------------------

def docx_blocks(path: Path) -> list[tuple[str, list[str]]]:
    """Top-level blocks of a .docx in order: ("p", [text]) or ("tbl", [paragraph texts])."""
    xml = zipfile.ZipFile(path).read("word/document.xml").decode("utf-8")
    body = re.search(r"<w:body>(.*)</w:body>", xml, flags=re.S).group(1)
    blocks = []
    for b in re.findall(r"<w:p[ >].*?</w:p>|<w:tbl>.*?</w:tbl>", body, flags=re.S):
        paras = re.findall(r"<w:p[ >].*?</w:p>", b, flags=re.S) if b.startswith("<w:tbl") else [b]
        # Only the text runs: drawings also carry numbers (their position) as element text.
        texts = [html.unescape("".join(re.findall(r"<w:t(?: [^>]*)?>([^<]*)</w:t>", p))) for p in paras]
        blocks.append(("tbl" if b.startswith("<w:tbl") else "p", texts))
    return blocks


_CAPTION = re.compile(r"^[（(]([^）)]+)[）)]$")
_TOC_ENTRY = re.compile(r"^\s*第\s*([０-９0-9]+)条（([^）]+)）\s*$")
_ARTICLE_NO = re.compile(r"^第\s*([０-９0-9]+)条\s*")


def _num(s: str) -> int:
    return int(unicodedata.normalize("NFKC", s))


def _boxed(blocks: list[tuple[str, list[str]]]) -> list[tuple[str, list[str]]]:
    """A few articles are plain paragraphs instead of a table: "（caption）",
    then "第N条…", up to the commentary's heading ("…】"). Folds each such run
    into one table-like block, so the tables and these read the same way."""
    out: list[tuple[str, list[str]]] = []
    i = 0
    while i < len(blocks):
        kind, texts = blocks[i]
        nxt = blocks[i + 1] if i + 1 < len(blocks) else None
        if (kind == "p" and _CAPTION.match(texts[0].strip()) and nxt and nxt[0] == "p"
                and _ARTICLE_NO.match(nxt[1][0])):
            run, i = [texts[0]], i + 1
            while i < len(blocks) and blocks[i][0] == "p" and not blocks[i][1][0].strip().endswith("】"):
                run.append(blocks[i][1][0])
                i += 1
            out.append(("tbl", run))
            continue
        out.append((kind, texts))
        i += 1
    return out


def model_rules_articles(blocks: list[tuple[str, list[str]]]) -> list[tuple[str, str, str]]:
    """The model rules put each article in a one-cell table (caption first)
    with the commentary in plain paragraphs around it. The article number is
    not always in the table, so it comes from the table of contents, which also
    tells which tables are the first alternative: walking the tables in order,
    a table is taken only when it is the next article the contents expect --
    by the number when the table has one, otherwise by the caption (the two
    captions differ slightly for a few articles, so the table's own is kept).
    """
    expected: list[tuple[int, str]] = []
    for kind, texts in blocks:
        if kind == "p" and (m := _TOC_ENTRY.match(texts[0])):
            n = _num(m.group(1))
            if n not in [e[0] for e in expected]:
                expected.append((n, m.group(2)))
        if kind == "tbl" and expected:
            break  # the contents end before the first article table
    out: list[tuple[str, str, str]] = []
    i = 0
    for kind, texts in _boxed(blocks):
        if kind != "tbl" or i == len(expected):
            continue
        texts = [t for t in texts if t.strip()]
        if len(texts) < 2 or not (m := _CAPTION.match(texts[0].strip())):
            continue
        caption, no = m.group(1).strip(), _ARTICLE_NO.match(texts[1])
        if (_num(no.group(1)) if no else caption) != (expected[i][0] if no else expected[i][1]):
            continue
        body = [_ARTICLE_NO.sub("", texts[1], count=1)] + texts[2:]
        out.append((f"第{expected[i][0]}条", caption, "\n".join(body)))
        i += 1
    missing = expected[i:]
    if missing:
        raise ValueError(f"articles not found in the body: {missing}")
    return out


_IKUJI_HEADING = re.compile(r"^第([０-９0-9]+)条（(.+)）$")


def childcare_rules_articles(paragraphs: list[str]) -> list[tuple[str, str, str]]:
    """Plain paragraphs: "第N条（caption）" then the article. An alternative
    starts with a 《…例》 line and runs to "※以下…" or to the next article."""
    out: list[tuple[str, str, str]] = []
    current: tuple[str, str, list[str]] | None = None
    skipping = False
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
        if m := _IKUJI_HEADING.match(p):
            if current:
                out.append((current[0], current[1], "\n".join(current[2])))
            current, skipping = (f"第{_num(m.group(1))}条", m.group(2), []), False
            continue
        if "《" in p:
            skipping = True
            continue
        if skipping:
            if p.startswith("※以下"):
                skipping = False
            continue
        if current and not p.startswith("（附則）"):
            current[2].append(p)
    if current:
        out.append((current[0], current[1], "\n".join(current[2])))
    return out


# --- driver -----------------------------------------------------------------

def fetch(src: Source) -> Path:
    path = SRC_DIR / f"{src.doc_id}{'.json' if src.kind == 'egov' else '.docx'}"
    if not path.exists():
        SRC_DIR.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(src.url) as r:
            path.write_bytes(r.read())
    return path


def build(src: Source) -> list[Chunk]:
    path = fetch(src)
    if src.kind == "egov":
        law = json.loads(path.read_bytes())
        version = f"{law['revision_info']['amendment_enforcement_date']}施行"
        articles = egov_articles(law)
    elif src.kind == "mhlw_model":
        version = "令和7年12月版"
        articles = model_rules_articles(docx_blocks(path))
    else:
        version = "令和7年4月1日・10月1日施行対応(簡易版)"
        articles = childcare_rules_articles([t for _, texts in docx_blocks(path) for t in texts])
    return [make_chunk(src, version, i + 1, *a) for i, a in enumerate(articles)]


def main() -> None:
    chunks = [c for src in SOURCES for c in build(src)]
    too_long = [c.id for c in chunks if len(c.text) > MAX_CHARS]
    if too_long:
        sys.exit(f"over {MAX_CHARS} characters, split these by paragraph: {too_long}")
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", encoding="utf-8", newline="\n") as f:
        for c in chunks:
            f.write(json.dumps(asdict(c), ensure_ascii=False) + "\n")
    for src in SOURCES:
        lengths = sorted(len(c.text) for c in chunks if c.doc_id == src.doc_id)
        print(f"{src.doc_id}: {len(lengths)} chunks, chars median {lengths[len(lengths) // 2]}, max {lengths[-1]}, total {sum(lengths)}")
    print(f"wrote {len(chunks)} chunks to {OUT_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
