#!/usr/bin/env python3
"""Local repository knowledge: browse, search, bounded reads and source drift checks.

Uses the JSON subset of YAML frontmatter so developer tooling needs only stdlib.
This checks the repository's OKF profile, not every possible OKF document.
"""
import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

REPO = Path(__file__).resolve().parents[1]
BUNDLE = Path("docs/knowledge")


def contained(root, path):
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("Path leaves repository: %s" % path)
    return resolved


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@dataclass
class Document:
    path: Path
    identifier: str
    metadata: dict
    body: str
    text: str
    body_line: int


def documents(root):
    bundle = root / BUNDLE
    result = []
    for path in sorted(bundle.rglob("*.md")):
        if path.name in ("index.md", "log.md"):
            continue
        contained(root, path)
        text = path.read_text(encoding="utf-8")
        match = re.match(r"\A---\n(.*?)\n---\n", text, re.S)
        if not match:
            raise ValueError("%s: missing frontmatter" % path.relative_to(root))
        try:
            metadata = json.loads(match.group(1))
        except ValueError as exc:
            raise ValueError("%s: use JSON (valid YAML) frontmatter for this local profile" % path) from exc
        if not isinstance(metadata, dict):
            raise ValueError("%s: frontmatter must be an object" % path)
        result.append(Document(path, path.relative_to(bundle).with_suffix("").as_posix(), metadata,
                               text[match.end():], text, text[:match.end()].count("\n") + 1))
    return result


def source_path(root, doc, resource):
    url = urlsplit(resource)
    if url.scheme in ("https", "http"):
        return None  # external references are recorded, never fetched by this tool
    if url.scheme or url.netloc or not url.path:
        raise ValueError("Unsupported source resource: %s" % resource)
    path = root / BUNDLE / url.path.lstrip("/") if url.path.startswith("/") else doc.path.parent / url.path
    return contained(root, path)


def source_issues(root, doc):
    errors = []
    for source in doc.metadata.get("sources", []):
        try:
            path = source_path(root, doc, source["resource"])
            if path is None:
                continue
            if not path.is_file():
                errors.append("missing source: " + source["resource"])
            elif source.get("sha256") != digest(path):
                errors.append("changed source: " + source["resource"])
        except (KeyError, TypeError, ValueError, OSError) as exc:
            errors.append(str(exc))
    return errors


def render_index(docs):
    lines = ['---', '{"okf_version": "0.2"}', '---', '# Repository knowledge', '',
             'Read the topic for your change, then its linked sources. Source fingerprints',
             'detect drift; they do not assert human review or semantic correctness.', '']
    groups = sorted({d.metadata.get("group", "Topics") for d in docs})
    for group in groups:
        lines += ["# " + group, ""]
        for doc in sorted(docs, key=lambda d: d.identifier):
            if doc.metadata.get("group", "Topics") == group:
                lines.append("* [%s](%s.md) - %s" % (doc.metadata["title"], doc.identifier,
                                                     doc.metadata["description"]))
        lines.append("")
    return "\n".join(lines)


def check(root, docs):
    errors = []
    if not docs:
        return ["Knowledge bundle has no concepts"]
    for doc in docs:
        m = doc.metadata
        prefix = doc.identifier + ": "
        for key in ("type", "title", "description"):
            if not isinstance(m.get(key), str) or not m[key].strip():
                errors.append(prefix + "missing string " + key)
        if not isinstance(m.get("tags"), list) or not all(isinstance(t, str) for t in m["tags"]):
            errors.append(prefix + "tags must be a list of strings")
        sources = m.get("sources")
        if not isinstance(sources, list) or not sources or not all(
                isinstance(s, dict) and isinstance(s.get("resource"), str) and isinstance(s.get("id"), str)
                for s in sources):
            errors.append(prefix + "sources need id and resource")
            continue
        ids = [s["id"] for s in sources]
        if len(ids) != len(set(ids)):
            errors.append(prefix + "duplicate source ids")
        cited = set(re.findall(r"\[\^([^\]]+)\](?!:)", doc.body))
        if cited - set(ids):
            errors.append(prefix + "unknown source footnotes: " + ", ".join(sorted(cited - set(ids))))
        errors += [prefix + e for e in source_issues(root, doc)]
        for link in re.findall(r"\[[^\]\n]*\]\(([^\s)]+)\)", doc.body):
            url = urlsplit(link)
            if url.scheme or not url.path:
                continue
            try:
                path = source_path(root, doc, unquote(link))
                if not path.exists():
                    errors.append(prefix + "broken link: " + link)
            except ValueError as exc:
                errors.append(prefix + str(exc))
    if not errors:
        index = root / BUNDLE / "index.md"
        if not index.is_file() or index.read_text(encoding="utf-8") != render_index(docs):
            errors.append("index.md is out of date; run: python3 tools/knowledge.py index --write")
    return errors


def search(docs, query, kind=None, tag=None):
    terms = set(re.findall(r"[^\W_]+", query.casefold()))
    results = []
    for doc in docs:
        m = doc.metadata
        if kind and m.get("type", "").casefold() != kind.casefold():
            continue
        if tag and tag.casefold() not in [t.casefold() for t in m.get("tags", [])]:
            continue
        title = m.get("title", "").casefold()
        description = m.get("description", "").casefold()
        tags = " ".join(m.get("tags", [])).casefold()
        body = doc.body.casefold()
        score = sum(10 * (t in title) + 6 * (t in tags) + 3 * (t in description) + min(body.count(t), 3)
                    for t in terms)
        if score:
            snippets = [(i, line.strip()[:180]) for i, line in enumerate(doc.body.splitlines(), doc.body_line)
                        if any(t in line.casefold() for t in terms) and line.strip()][:2]
            results.append((score, doc, snippets))
    return sorted(results, key=lambda item: (-item[0], item[1].identifier))


def main(argv=None, repo_root=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list concept titles and descriptions")
    query = sub.add_parser("search", help="rank concepts; return matching line snippets")
    query.add_argument("query", nargs="+")
    query.add_argument("--type", dest="kind")
    query.add_argument("--tag")
    query.add_argument("--limit", type=int, default=5, choices=range(1, 21))
    read = sub.add_parser("read", help="read a bounded body excerpt with actual file line numbers")
    read.add_argument("topic")
    read.add_argument("--start", type=int, default=1)
    read.add_argument("--lines", type=int, default=60, choices=range(1, 201))
    index = sub.add_parser("index", help="render metadata-derived navigation")
    index.add_argument("--write", action="store_true")
    stamp = sub.add_parser("stamp", help="update source fingerprints AFTER reviewing and updating prose")
    stamp.add_argument("topic", help="concept id or 'all'; does not add verified/human-review claims")
    sub.add_parser("check", help="check profile, local sources, links and generated index")
    args = parser.parse_args(argv)
    root = (Path(repo_root) if repo_root else REPO).resolve()
    try:
        docs = documents(root)
        by_id = {d.identifier: d for d in docs}
        if args.command == "check":
            errors = check(root, docs)
            for error in errors:
                print(error, file=sys.stderr)
            if not errors:
                print("OK: %d concepts; local links, source fingerprints and index checked. Prose is not attested." % len(docs))
            return int(bool(errors))
        if args.command == "index":
            text = render_index(docs)
            if args.write:
                (root / BUNDLE / "index.md").write_text(text, encoding="utf-8")
            else:
                print(text, end="")
        elif args.command == "list":
            for doc in docs:
                print("%s [%s] - %s" % (doc.identifier, doc.metadata.get("type"), doc.metadata.get("description")))
        elif args.command == "search":
            hits = search(docs, " ".join(args.query), args.kind, args.tag)[:args.limit]
            for _, doc, snippets in hits:
                state = "CHANGED SOURCES" if source_issues(root, doc) else "sources unchanged"
                print("%s: %s [%s]" % (doc.path.relative_to(root), doc.metadata.get("title"), state))
                for line, snippet in snippets:
                    print("  %d: %s" % (line, snippet))
            if not hits:
                print("No matching concepts; try another term or inspect: python3 tools/knowledge.py list")
        elif args.command == "read":
            doc = by_id[args.topic.removesuffix(".md")]
            state = source_issues(root, doc)
            print("%s [%s]" % (doc.path.relative_to(root), "; ".join(state) if state else "sources unchanged"))
            start = max(doc.body_line, args.start)
            for line, text in enumerate(doc.text.splitlines(), 1):
                if start <= line < start + args.lines:
                    print("%d: %s" % (line, text))
        elif args.command == "stamp":
            selected = docs if args.topic == "all" else [by_id[args.topic.removesuffix(".md")]]
            for doc in selected:
                for source in doc.metadata["sources"]:
                    path = source_path(root, doc, source["resource"])
                    if path is not None:
                        source["sha256"] = digest(path)
                doc.path.write_text("---\n" + json.dumps(doc.metadata, ensure_ascii=False, indent=2) + "\n---\n" + doc.body,
                                    encoding="utf-8")
                print("Stamped %s; source hashes only, no semantic verification asserted." % doc.identifier)
        return 0
    except (ValueError, KeyError, OSError, TypeError) as exc:
        print("Knowledge catalog error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
