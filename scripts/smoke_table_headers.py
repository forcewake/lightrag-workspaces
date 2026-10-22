#!/usr/bin/env python3
"""Check the real pinned Markdown writer/chunker offline using synthetic data.

Runs a disposable container with no network, ports, host data mounts or model
services. --module-file optionally supplies a local hook before building an
image; omit it when checking that the built image packages the hook correctly.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import time


RUNTIME_CHECKS = r'''
import hashlib, json, pathlib, re, socket, tempfile
from table_headers import install_table_header_preservation

def no_network(*args, **kwargs):
    raise AssertionError("Network access attempted in offline table-header check")
socket.socket.connect = no_network
socket.create_connection = no_network

from lightrag.parser.markdown.parser import NativeMarkdownParser
from lightrag.sidecar import write_sidecar
from lightrag.chunker.paragraph_semantic import chunking_by_paragraph_semantic

class CharacterTokenizer:
    def encode(self, text): return [ord(c) for c in text]
    def decode(self, tokens): return ''.join(chr(c) for c in tokens)

checks = []
def check(name, condition):
    if not condition: raise AssertionError(name)
    checks.append(name)

check('pinned hook installs', install_table_header_preservation() is True)
check('hook installation is idempotent', install_table_header_preservation() is False)
tokenizer = CharacterTokenizer()
with tempfile.TemporaryDirectory(prefix='table-headers-') as temporary:
    root = pathlib.Path(temporary)
    def parse(name, markdown, cap=4096):
        parser = NativeMarkdownParser()
        blocks, _, meta = parser._extract_text(markdown, bundle_root=None)
        ir = parser.build_ir(blocks, document_name=name+'.md', asset_dir_name=name+'.blocks.assets', metadata=meta)
        folder = root/name
        parsed = write_sidecar(ir, parsed_dir=folder, doc_id='doc-'+hashlib.md5(name.encode()).hexdigest(), engine='native')
        block_path = folder/(name+'.blocks.jsonl')
        rows = [json.loads(line) for line in block_path.read_text().splitlines()]
        content_rows = [r for r in rows if r['type']=='content']
        merged = '\n\n'.join(r['content'] for r in content_rows)
        tables = json.loads((folder/(name+'.tables.json')).read_text())['tables']
        chunks = chunking_by_paragraph_semantic(tokenizer, merged, cap, blocks_path=str(block_path),
                                                chunk_overlap_token_size=0, drop_references=False)
        check(name+': real paragraph chunks exist', bool(chunks))
        check(name+': token counts and caps', all(c['tokens']==len(tokenizer.encode(c['content']))<=cap for c in chunks))
        block_ids = {r['blockid'] for r in content_rows}
        check(name+': chunk provenance resolves', all(c.get('sidecar', {}).get('refs') and
              all(r['type']=='block' and r['id'] in block_ids for r in c['sidecar']['refs']) for c in chunks))
        check(name+': writer body agrees with returned content', merged in parsed['content'])
        return merged, tables, chunks

    text = '| RevenueEUR | CustomerName |\n| --- | --- |\n| 42 | Alice |\n'
    merged, tables, chunks = parse('small', text)
    table, = tables.values()
    check('small: header in stored table body', json.loads(table['content']) == [['RevenueEUR','CustomerName'],['42','Alice']])
    check('small: header metadata retained', json.loads(table['table_header']) == [['RevenueEUR','CustomerName']])
    check('small: searchable without table analysis or t option', all(word in '\n'.join(c['content'] for c in chunks)
          for word in ('RevenueEUR','CustomerName','42','Alice')) and 'llm_analyze_result' not in table)
    check('small: one body header occurrence', merged.count('RevenueEUR') == 1)

    merged, tables, chunks = parse('header-only', '| OnlyHeader |\n| --- |\n')
    table, = tables.values()
    check('header-only: retained and searchable', json.loads(table['content']) == [['OnlyHeader']] and
          any('OnlyHeader' in c['content'] for c in chunks))
    check('header-only: dimensions include indexed header', table['dimension'] == [1,1])

    merged, tables, chunks = parse('equal-row', '| Same |\n| --- |\n| Same |\n| Other |\n')
    table, = tables.values()
    check('equal-row: genuine repeated data row retained', json.loads(table['content']) == [['Same'],['Same'],['Other']])

    expected = [[f'row-{i:03}', 'payload-'+str(i)+'x'*18] for i in range(50)]
    markdown = '| HeaderKey | HeaderValue |\n| --- | --- |\n'+''.join('| '+a+' | '+b+' |\n' for a,b in expected)
    merged, tables, chunks = parse('large', markdown, 512)
    found = []
    table_slices = 0
    for chunk in chunks:
        for body in re.findall(r'<table [^>]+>(.*?)</table>', chunk['content']):
            rows = json.loads(body)
            check('large: slice header recovered once '+str(table_slices), rows[0] == ['HeaderKey','HeaderValue'] and
                  rows[1:] and all(row != ['HeaderKey','HeaderValue'] for row in rows[1:]))
            found.extend(rows[1:]); table_slices += 1
    check('large: several bounded table slices', table_slices > 1)
    check('large: data rows preserved in source order', found == expected)

    giant_header = 'LongHeader'+('Z'*900)
    merged, tables, chunks = parse('giant', '| '+giant_header+' |\n| --- |\n| DataEnd |\n', 512)
    check('giant: recursive fragments retain complete body text',
          ''.join(c['content'] for c in chunks).replace(' ', '').find(giant_header) >= 0 and
          any('DataEnd' in c['content'] for c in chunks))

    html = '<table><thead><tr><th colspan="2">MergedHeader</th></tr></thead><tbody><tr><td>A</td><td>B</td></tr></tbody></table>'
    merged, tables, chunks = parse('html', html)
    table, = tables.values()
    check('html: thead and spans kept without duplicate', merged.count('MergedHeader') == 1 and
          table['content'].count('MergedHeader') == 1 and 'colspan="2"' in merged and
          table['table_header'].count('MergedHeader') == 1)

print(json.dumps({'passed':True, 'checks':checks, 'provider_calls':0, 'network':'none',
                  'tokenizer':'deterministic character tokenizer; real pinned parser/writer/chunker',
                  'data_scope':'synthetic temporary directory only'}))
'''


def smoke(engine: str, image: str, memory: str = "2g", module_file: Path | None = None) -> dict:
    program = RUNTIME_CHECKS
    if module_file:
        source = module_file.read_text()
        program = ("import sys, pathlib\n"
                   "hook_root=pathlib.Path('/tmp/workspaces-table-headers'); hook_root.mkdir()\n"
                   f"(hook_root/'table_headers.py').write_text({source!r})\n"
                   "sys.path.insert(0,str(hook_root))\n" + program)
    started = time.monotonic()
    command = [engine, "run", "--rm", "-i", "--network", "none", "--read-only",
               "--tmpfs", "/tmp:rw,nosuid,nodev,size=128m", "--memory", memory,
               "--cpus", "2", "--pids-limit", "128", "-e", "PYTHONDONTWRITEBYTECODE=1",
               "-e", "HOME=/tmp", "--entrypoint", "python", image, "-"]
    process = subprocess.run(command, input=program, capture_output=True, text=True, timeout=180)
    if process.returncode:
        raise RuntimeError("Offline table-header container check failed:\n"+process.stderr[-6000:])
    result = json.loads(process.stdout.strip().splitlines()[-1])
    result.update(engine=engine, image=image, seconds=round(time.monotonic()-started, 2),
                  hook_source="explicit local module" if module_file else "packaged image module")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("docker", "podman"), default="docker")
    parser.add_argument("--image", required=True)
    parser.add_argument("--memory", default="2g")
    parser.add_argument("--module-file", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = smoke(args.engine, args.image, args.memory, args.module_file)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        result = {"passed":False, "error":str(error), "network":"none"}
    if args.output:
        args.output.write_text(json.dumps(result, indent=2)+"\n")
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
