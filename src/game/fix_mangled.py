import re

with open('src/recomp/gen/recomp_0008.c', 'r', encoding='utf-8', errors='ignore') as f:
    content = f.read()

# Find the mangled section in sub_0013B0E0 - use a more flexible approach
# Find from "loc_0013B16D: ;" to the next "edx = MEM32(esi + 0x104);"
start_marker = "loc_0013B16D: ;"
end_marker = "edx = MEM32(esi + 0x104);"

start_idx = content.find(start_marker)
if start_idx < 0:
    print("Could not find start marker")
    exit(1)

# Find the second occurrence of end_marker after start_idx
end_idx = content.find(end_marker, start_idx)
if end_idx < 0:
    print("Could not find end marker")
    exit(1)

# Include the end marker line
end_idx = content.find('\n', end_idx) + 1

print(f"Found section from {start_idx} to {end_idx}")
print(f"Section length: {end_idx - start_idx}")

# Show the section
section = content[start_idx:end_idx]
print("--- CURRENT SECTION ---")
print(section)
print("--- END SECTION ---")

new_section = '''loc_0013B16D: ;
    ecx = MEM32(esi + 0x104);
    /* Manual guard (not in original x86): ecx should be a bounded loop
     * index (0..0x3F, matching the 64-iteration cap this loop already
     * enforces via edi/0x40 below) into a fixed-size array at esi. In a
     * corrupted run it can instead hold an unrelated garbage value
     * (observed: a float bit pattern), which turns "esi + ecx*4" into a
     * wild address. Same defensive-bounds philosophy as the other guards
     * in this session - see DEBUGGING_NOTES.md. */
    /* Float bit patterns like 0x3F800000 (1.0f) look like a valid index
     * (0x3F800000 < 0x40 is false) but they are clearly not small loop
     * indices - they are floating-point bit patterns from corrupted vtables.
     * Skip the work when ecx is in the float range [0x3F000000, 0x3F800001). */
    if (ecx >= 0x3F000000u && ecx <= 0x3F800000u) { goto loc_0013B192; }
    if (ecx >= 0x40u) goto loc_0013B192;  /* Skip invalid indices */
    if (ecx < 0x40u) MEM32(esi + ecx * 4) = edi;
edx = MEM32(esi + 0x104);'''

if section.strip() != new_section.strip():
    content = content[:start_idx] + new_section + content[end_idx:]
    with open('src/recomp/gen/recomp_0008.c', 'w', encoding='utf-8') as f:
        f.write(content)
    print("Fixed mangled section!")
else:
    print("Section already matches expected content")