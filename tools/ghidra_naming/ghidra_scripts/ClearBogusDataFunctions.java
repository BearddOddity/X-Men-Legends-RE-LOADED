// ClearBogusDataFunctions.java - undo functions that the RTTI walk created on
// top of data.
//
// The unbounded vtable walk ran off the end of each vtable and called
// createFunction on whatever dword came next. When that dword was part of an
// RTTI structure in .rdata/.data, Ghidra disassembled the structure as code
// and wrapped it in a function. AuditVtableBounds took the misleading name
// away; this takes the code away.
//
// Input:  /mnt/share/vtable_strip.txt  the OVERRUN rows written by the strip
// Output: /mnt/share/cleared_data_funcs.txt
//
// Only touches functions whose entry is outside the code blocks, so a real
// function can never be cleared by it.
//
// @category Recomp

import java.io.PrintWriter;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.address.AddressSet;
import ghidra.program.model.listing.Function;
import ghidra.program.model.mem.MemoryBlock;

public class ClearBogusDataFunctions extends GhidraScript {

    @Override
    public void run() throws Exception {
        String in = "/mnt/share/vtable_strip.txt";
        String out = "/mnt/share/cleared_data_funcs.txt";
        String[] args = getScriptArgs();
        if (args.length > 0) in = args[0];
        if (args.length > 1) out = args[1];

        // Blocks that hold real code. Anything named here is off limits.
        Set<String> codeBlocks = new HashSet<>();
        codeBlocks.add(".text");
        for (MemoryBlock b : currentProgram.getMemory().getBlocks()) {
            // The XBE loader marks nearly everything executable, so the flag is
            // useless; go by the block that the entry point lives in plus the
            // library blocks that genuinely contain code.
            if (b.getName().equals(".rdata") || b.getName().equals(".data")
                    || b.getName().startsWith("$$")) {
                continue;
            }
            codeBlocks.add(b.getName());
        }
        println("code blocks (never cleared): " + codeBlocks);

        Set<Address> wanted = new HashSet<>();
        for (String line : Files.readAllLines(Paths.get(in))) {
            if (!line.startsWith("OVERRUN")) continue;
            String[] f = line.split("\t");
            if (f.length < 2) continue;
            wanted.add(toAddr(Long.parseLong(f[1].trim(), 16)));
        }
        println("overrun rows read: " + wanted.size());

        PrintWriter w = new PrintWriter(out);
        int cleared = 0, skippedCode = 0, gone = 0, failed = 0;

        for (Address a : wanted) {
            if (monitor.isCancelled()) break;
            MemoryBlock b = currentProgram.getMemory().getBlock(a);
            if (b == null) { gone++; continue; }
            if (codeBlocks.contains(b.getName())) { skippedCode++; continue; }

            Function f = getFunctionAt(a);
            if (f == null) { gone++; continue; }
            Address min = f.getBody().getMinAddress();
            Address max = f.getBody().getMaxAddress();
            try {
                removeFunction(f);
                clearListing(new AddressSet(min, max));
                w.printf("CLEARED\t%s\t%s\t%s-%s%n", a, b.getName(), min, max);
                cleared++;
            } catch (Exception e) {
                w.printf("FAILED\t%s\t%s%n", a, e.getMessage());
                failed++;
            }
        }

        w.printf("# cleared=%d skipped_in_code=%d already_gone=%d failed=%d%n",
                cleared, skippedCode, gone, failed);
        w.close();
        println("cleared=" + cleared + " skipped_in_code=" + skippedCode
                + " already_gone=" + gone + " failed=" + failed + " -> " + out);
    }
}
