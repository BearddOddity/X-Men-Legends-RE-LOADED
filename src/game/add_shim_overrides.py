#!/usr/bin/env python3
"""Add D3D8 shim functions to recomp_lookup_manual"""

import re

with open('src/recomp_manual.c', 'r', encoding='utf-8') as f:
    content = f.read()

# Extract shim function addresses from d3d8_shim.c
with open('src/d3d8_shim.c', 'r', encoding='utf-8') as f:
    shim_content = f.read()

# Find all sub_0035xxxx functions in the shim
shim_funcs = re.findall(r'void (sub_0035[0-9A-F]+)\(void\)', shim_content)
print(f"Found {len(shim_funcs)} shim functions:")
for f in shim_funcs:
    print(f"  {f}")

# Find the recomp_lookup_manual function
lookup_start = content.find('recomp_func_t recomp_lookup_manual(uint32_t xbox_va)')
if lookup_start < 0:
    print("Could not find recomp_lookup_manual")
    exit(1)

# Find the end of the function (the return 0; line)
func_start = content.rfind('\n', 0, lookup_start) + 1
func_end = content.find('return (recomp_func_t)0;', lookup_start)
if func_end < 0:
    print("Could not find end of recomp_lookup_manual")
    exit(1)
func_end = content.find('\n', func_end) + 1

print(f"Function from {func_start} to {func_end}")

# Build the new if statements
new_entries = []
for func in shim_funcs:
    # Extract address from function name
    addr = func[4:]  # remove 'sub_'
    new_entries.append(f'    if (xbox_va == 0x{addr}u) return {func};')

# Insert before the final return 0
insert_pos = func_end - 1  # before the newline before return 0
new_content = content[:insert_pos] + '\n' + '\n'.join(new_entries) + '\n' + content[insert_pos:]

# Also need to add extern declarations at the top of the function
# Find where the extern declarations end
extern_end = content.rfind('extern void ', 0, lookup_start)
if extern_end < 0:
    extern_end = content.find('recomp_func_t recomp_lookup_manual', 0, lookup_start)
    if extern_end < 0:
        print("Could not find extern section")
        exit(1)

# Find the last extern void line
extern_lines = []
for m in re.finditer(r'extern void (sub_00[0-9A-F]+)\(void\);', content[:lookup_start]):
    extern_lines.append(m.group(1))

print(f"Found {len(extern_lines)} existing extern declarations")

# Add new extern declarations
new_externs = []
for func in shim_funcs:
    if func not in extern_lines:
        new_externs.append(f'extern void {func}(void);')

if new_externs:
    # Insert after the last extern
    last_extern = content.rfind('extern void sub_', 0, lookup_start)
    if last_extern >= 0:
        last_extern_end = content.find('\n', last_extern) + 1
        new_content = new_content[:last_extern_end] + '\n'.join(new_externs) + '\n' + new_content[last_extern_end:]
    else:
        print("Could not find where to insert externs")

with open('src/recomp_manual.c', 'w', encoding='utf-8') as f:
    f.write(new_content)

print("Updated recomp_manual.c with D3D8 shim overrides")