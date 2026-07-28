#!/usr/bin/env python3
import json, bisect, os, sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, '..', '..'))

UNRESOLVED_PATH = os.path.join(SCRIPT_DIR, 'output', 'unresolved_symbols.txt')
FUNCTIONS_PATH = os.path.join(REPO_ROOT, 'tools', 'disasm', 'output', 'functions.json')
OUTPUT_PATH = os.path.join(SCRIPT_DIR, 'output', 'missing_functions.json')

# SECTIONS/TEXT_START/TEXT_END are populated per-run in main() from the
# target XBE's own section table (via --xbe-json), never hardcoded here.
# A previous version hardcoded one game's layout (sizes/section names like
# XMV/XONLINE/XNET), which silently misclassified every other game's real
# .text addresses as fake "library sections" and dropped them from the
# gap/continuation seed candidates - see DEBUGGING_NOTES.md in xmen-legends.
SECTIONS = []
TEXT_START = None
TEXT_END = None


def _load_sections(xbe_json_path):
    with open(xbe_json_path) as f:
        xbe = json.load(f)
    sections = []
    for s in xbe['sections']:
        sections.append({
            'name': s['name'],
            'va': int(s['virtual_addr'], 16),
            'size': s['virtual_size'],
        })
    return sections


def find_section(addr):
    for s in SECTIONS:
        if s['va'] <= addr < s['va'] + s['size']:
            return s['name']
    return None


def main():
    global SECTIONS, TEXT_START, TEXT_END

    if len(sys.argv) < 2:
        print('Usage: python -m tools.recomp.analyze_unresolved <xbe_analysis.json>', file=sys.stderr)
        print('  <xbe_analysis.json> is the --json output of tools.xbe_parser for the target XBE.', file=sys.stderr)
        sys.exit(1)

    SECTIONS = _load_sections(sys.argv[1])
    text = next(s for s in SECTIONS if s['name'] == '.text')
    TEXT_START = text['va']
    TEXT_END = text['va'] + text['size']
    print(f"Loaded {len(SECTIONS)} sections from {sys.argv[1]}")
    print(f"  .text: 0x{TEXT_START:08X} - 0x{TEXT_END:08X}")
    print()

    print('Loading function database...')
    with open(FUNCTIONS_PATH) as f:
        raw_funcs = json.load(f)

    functions = []
    for func in raw_funcs:
        start = int(func['start'], 16)
        end = int(func['end'], 16)
        name = func['name']
        functions.append((start, end, name))

    functions.sort(key=lambda x: x[0])
    func_starts = [f[0] for f in functions]
    print(f'  Loaded {len(functions)} functions')
    print(f'  Range: 0x{functions[0][0]:08X} - 0x{functions[-1][1]:08X}')

    print('Loading unresolved symbols...')
    with open(UNRESOLVED_PATH) as f:
        unresolved = []
        for line in f:
            line = line.strip()
            if not line: continue
            addr = int(line.replace('sub_', ''), 16)
            unresolved.append(addr)
    unresolved.sort()
    print(f'  Loaded {len(unresolved)} unresolved symbols')
    print(f'  Range: 0x{unresolved[0]:08X} - 0x{unresolved[-1]:08X}')

    results = {
        'mid_function': [],
        'continuation': [],
        'gap': [],
        'library_section': [],
        'data_section': [],
        'unknown': [],
    }
    missing_functions = []

    for addr in unresolved:
        section = find_section(addr)

        if section is None:
            results['unknown'].append(addr)
            missing_functions.append({'address': f'0x{addr:08X}', 'type': 'unknown', 'estimated_end': None})
            continue

        if section in ('.rdata', '.data', '.data1'):
            results['data_section'].append(addr)
            missing_functions.append({'address': f'0x{addr:08X}', 'type': 'data_section', 'section': section, 'estimated_end': None})
            continue

        if section != '.text':
            sec_info = next(s for s in SECTIONS if s['name'] == section)
            sec_end = sec_info['va'] + sec_info['size']
            results['library_section'].append((addr, section))
            missing_functions.append({'address': f'0x{addr:08X}', 'type': 'library_section', 'section': section, 'estimated_end': f'0x{sec_end:08X}'})
            continue

        idx = bisect.bisect_right(func_starts, addr) - 1

        if idx < 0:
            next_start = func_starts[0] if func_starts else TEXT_END
            results['gap'].append((addr, TEXT_START, next_start, next_start - addr))
            missing_functions.append({'address': f'0x{addr:08X}', 'type': 'gap', 'next_func_start': f'0x{next_start:08X}', 'estimated_end': f'0x{next_start:08X}', 'gap_size': next_start - addr})
            continue

        func_start, func_end, func_name = functions[idx]

        if func_start < addr < func_end:
            results['mid_function'].append((addr, func_start, func_name))
            missing_functions.append({'address': f'0x{addr:08X}', 'type': 'mid_function', 'parent_func': f'0x{func_start:08X}', 'parent_name': func_name, 'offset_into_func': addr - func_start, 'estimated_end': f'0x{func_end:08X}'})
            continue

        if addr == func_end:
            next_idx = idx + 1
            next_start = functions[next_idx][0] if next_idx < len(functions) else TEXT_END
            gap_to_next = next_start - addr
            results['continuation'].append((addr, func_start, func_name, gap_to_next))
            missing_functions.append({'address': f'0x{addr:08X}', 'type': 'continuation', 'parent_func': f'0x{func_start:08X}', 'parent_name': func_name, 'estimated_end': f'0x{next_start:08X}', 'gap_size': gap_to_next})
            continue

        if addr == func_start:
            print(f'  WARNING: Unresolved 0x{addr:08X} matches known function {func_name}')
            continue

        next_idx = idx + 1
        next_start = functions[next_idx][0] if next_idx < len(functions) else TEXT_END

        if func_end <= addr < next_start:
            gap_size = next_start - addr
            results['gap'].append((addr, func_end, next_start, gap_size))
            missing_functions.append({'address': f'0x{addr:08X}', 'type': 'gap', 'prev_func_end': f'0x{func_end:08X}', 'next_func_start': f'0x{next_start:08X}', 'estimated_end': f'0x{next_start:08X}', 'gap_size': gap_size})
            continue

        print(f'  WARNING: Could not classify 0x{addr:08X}')
        results['unknown'].append(addr)

    # Summary
    print()
    print('================================================================================')
    print('UNRESOLVED SYMBOL ANALYSIS SUMMARY')
    print('================================================================================')
    total = len(unresolved)
    print(f'\nTotal unresolved symbols: {total}')
    print()

    mid = results['mid_function']
    print(f'(a) MID-FUNCTION ENTRIES: {len(mid)} ({100*len(mid)/total:.1f}%)')
    print('    These addresses fall inside a known function body.')
    print('    Likely: tail-call targets, computed jumps, or function pointer offsets.')
    if mid:
        print('    Examples:')
        for addr, parent, name in mid[:10]:
            offset = addr - parent
            print(f'      0x{addr:08X}  (inside {name}, offset +0x{offset:X})')
    print()

    cont = results['continuation']
    print(f'(b) CONTINUATION PAST BOUNDARY: {len(cont)} ({100*len(cont)/total:.1f}%)')
    print('    Address falls exactly at a known function end.')
    print('    The function database underestimated the function size.')
    if cont:
        print('    Examples:')
        for addr, parent, name, gap in cont[:10]:
            print(f'      0x{addr:08X}  (after {name} ends, gap to next: {gap} bytes)')
        gaps = [g for _, _, _, g in cont]
        print(f'    Gap size stats: min={min(gaps)}, max={max(gaps)}, median={sorted(gaps)[len(gaps)//2]}, mean={sum(gaps)/len(gaps):.0f}')
    print()

    gap = results['gap']
    print(f'(c) GAP FUNCTIONS: {len(gap)} ({100*len(gap)/total:.1f}%)')
    print('    Addresses fall in gaps between known functions in .text.')
    print('    These are likely real functions the disassembler missed.')
    if gap and isinstance(gap[0], tuple):
        print('    Examples:')
        for item in gap[:10]:
            if len(item) == 4:
                a, pe, ns, gs = item
                print(f'      0x{a:08X}  (gap: 0x{pe:08X}-0x{ns:08X}, {gs} bytes)')
        gap_sizes = [item[3] for item in gap if isinstance(item, tuple) and len(item) == 4]
        if gap_sizes:
            print(f'    Gap size stats: min={min(gap_sizes)}, max={max(gap_sizes)}, median={sorted(gap_sizes)[len(gap_sizes)//2]}, mean={sum(gap_sizes)/len(gap_sizes):.0f}')
            print('    Gap size distribution:')
            for label, lo, hi in [('1-16 bytes',1,16),('17-64 bytes',17,64),('65-256 bytes',65,256),('257-1024 bytes',257,1024),('1025+ bytes',1025,999999999)]:
                count = sum(1 for s in gap_sizes if lo <= s <= hi)
                print(f'      {label:20s}: {count:5d}')
    print()

    lib = results['library_section']
    print(f'(d) LIBRARY SECTION FUNCTIONS: {len(lib)} ({100*len(lib)/total:.1f}%)')
    print('    Addresses fall in non-.text code sections (XDK libraries).')
    if lib:
        section_counts = {}
        for _, sec in lib:
            section_counts[sec] = section_counts.get(sec, 0) + 1
        print('    By section:')
        for sec, count in sorted(section_counts.items(), key=lambda x: -x[1]):
            sec_info = next(s for s in SECTIONS if s['name'] == sec)
            va = sec_info['va']
            sz = sec_info['size']
            print(f'      {sec:12s}: {count:4d} functions (section: 0x{va:08X}, {sz:,} bytes)')
        print('    Examples:')
        for a, sec in lib[:10]:
            print(f'      0x{a:08X}  ({sec})')
    print()

    data = results['data_section']
    print(f'(e) DATA SECTION REFERENCES: {len(data)} ({100*len(data)/total:.1f}%)')
    print('    Addresses fall in .rdata/.data - not code, likely misidentified.')
    if data:
        print('    Examples:')
        for a in data[:10]:
            sec = find_section(a)
            print(f'      0x{a:08X}  ({sec})')
    print()

    unk = results['unknown']
    if unk:
        print(f'(f) UNKNOWN (outside all sections): {len(unk)}')
        for a in unk[:5]:
            print(f'      0x{a:08X}')
    print()

    # Actionable summary
    print('================================================================================')
    print('ACTIONABLE SUMMARY')
    print('================================================================================')
    text_actionable = len(gap) + len(cont)
    print(f'\n  Symbols in .text that can extend function DB: {text_actionable} (gap: {len(gap)}, continuation: {len(cont)})')
    print(f'  Symbols in library sections (need lib disassembly):  {len(lib)}')
    print(f'  Mid-function entries (need parent func extension):   {len(mid)}')
    print(f'  Data section refs (need investigation):              {len(data)}')
    print(f'  Unknown:                                             {len(unk)}')
    print()

    if gap and isinstance(gap[0], tuple):
        unique_gaps = set()
        for item in gap:
            if len(item) == 4:
                _, pe, ns, _ = item
                unique_gaps.add((pe, ns))
        print(f'  Unique inter-function gaps with unresolved symbols: {len(unique_gaps)}')
        total_gap_bytes = sum(ns - pe for pe, ns in unique_gaps)
        print(f'  Total bytes in those gaps: {total_gap_bytes:,} ({total_gap_bytes/1024:.1f} KB)')

    if cont:
        unique_parents = set(parent for _, parent, _, _ in cont)
        print(f'  Functions needing boundary extension: {len(unique_parents)}')
        total_cont_bytes = sum(g for _, _, _, g in cont)
        print(f'  Total continuation bytes: {total_cont_bytes:,} ({total_cont_bytes/1024:.1f} KB)')

    if lib:
        lib_sections_used = set(sec for _, sec in lib)
        total_lib_bytes = sum(next(s for s in SECTIONS if s['name'] == sec)['size'] for sec in lib_sections_used)
        print(f'  Total library section bytes (all used sections): {total_lib_bytes:,} ({total_lib_bytes/1024:.1f} KB)')
    print()

    print(f'\nWriting {len(missing_functions)} entries to {OUTPUT_PATH}...')
    with open(OUTPUT_PATH, 'w') as f:
        json.dump(missing_functions, f, indent=2)
    print('Done.')

    quick_add_path = os.path.join(SCRIPT_DIR, 'output', 'addable_functions.json')
    addable = []
    for entry in missing_functions:
        if entry['type'] in ('gap', 'continuation', 'library_section'):
            addable.append({
                'address': entry['address'],
                'type': entry['type'],
                'estimated_end': entry.get('estimated_end'),
                'gap_size': entry.get('gap_size'),
                'section': entry.get('section', '.text'),
            })
    print(f'Writing {len(addable)} addable function candidates to {quick_add_path}...')
    with open(quick_add_path, 'w') as f:
        json.dump(addable, f, indent=2)
    print('Done.')


if __name__ == '__main__':
    main()
