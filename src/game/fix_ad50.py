import re

with open('src/recomp/gen/recomp_0008.c', 'r', encoding='utf-8', errors='ignore') as f:
    content = f.read()

# Fix sub_0013AD50 else block - the mangled comment
old = '''    } else {
    /* Manual addition (not in original x86): when construction is
     * skipped above, explicitly zero the output slot instead of
     * leaving it holding whatever garbage was there before this
     * object's own allocation - consumers elsewhere assume "0 or a
     * real pointer", not "uninitialized". See DEBUGGING_NOTES.md. */
    /* Manual guard (not in original x86): esi traces back to MEM32(esp + 0xC)
     * above, which is the saved esi from the function prologue. When the stack
     * has drifted into .data (D3D-null pattern), this can hold garbage instead
     * of a real object pointer. Writing through a garbage pointer faults.
     * Skip the zero-init when esi doesn't look like a real heap pointer.
     * Range matches every other guard in this tree; see tools_data/add_guard.py. */
    if (esi >= 0x00880000u && esi < 0x04000000u) { MEM32(esi) = 0; }
    }'''

new = '''    } else {
    /* Manual addition (not in original x86): when construction is
     * skipped above, explicitly zero the output slot instead of
     * leaving it holding whatever garbage was there before this
     * object's own allocation - consumers elsewhere assume "0 or a
     * real pointer", not "uninitialized". See DEBUGGING_NOTES.md. */
    /* Manual guard (not in original x86): esi traces back to MEM32(esp + 0xC)
     * above, which is the saved esi from the function prologue. When the stack
     * has drifted into .data (D3D-null pattern), this can hold garbage instead
     * of a real object pointer. Writing through a garbage pointer faults.
     * Skip the zero-init when esi doesn't look like a real heap pointer.
     * Range matches every other guard in this tree; see tools_data/add_guard.py. */
    if (esi >= 0x00880000u && esi < 0x04000000u) { MEM32(esi) = 0; }
    }'''

if old in content:
    content = content.replace(old, new)
    with open('src/recomp/gen/recomp_0008.c', 'w', encoding='utf-8') as f:
        f.write(content)
    print("Fixed sub_0013AD50 else block!")
else:
    print("Could not find the exact old text for sub_0013AD50")
    # Try to find it
    idx = content.find("} else {")
    if idx >= 0:
        # Find the specific else block in sub_0013AD50
        idx2 = content.find("Manual addition (not in original x86): when construction is", idx)
        if idx2 >= 0:
            print(f"Found at index {idx2}")
            print(content[idx2:idx2+500])