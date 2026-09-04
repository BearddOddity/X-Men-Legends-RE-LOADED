// Seed functions at addresses discovered by find_data_function_pointers.py.
//
// The recompiler's function list is built by following calls from the entry
// point, so a function referenced only by a dword in a vtable or callback
// table is never discovered. That script finds those targets; this applies
// them to the Ghidra database so the decompiler covers the same code the
// recompiler lifts.
//
// Input:  /mnt/share/seed_addrs.txt   one hex address per line
// Output: /mnt/share/seed_result.txt  one line per address, tab separated
//
// @category Recomp

import java.io.PrintWriter;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.List;

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.mem.MemoryBlock;

public class SeedDataFunctions extends GhidraScript {

    @Override
    public void run() throws Exception {
        String in = "/mnt/share/seed_addrs.txt";
        String out = "/mnt/share/seed_result.txt";
        String[] args = getScriptArgs();
        if (args.length > 0) in = args[0];
        if (args.length > 1) out = args[1];

        List<String> lines = Files.readAllLines(Paths.get(in));
        PrintWriter w = new PrintWriter(out);
        int created = 0, existed = 0, inside = 0, failed = 0, nonexec = 0;

        for (String line : lines) {
            String s = line.trim();
            if (s.isEmpty() || s.startsWith("#")) continue;
            if (s.startsWith("0x") || s.startsWith("0X")) s = s.substring(2);

            Address a = toAddr(Long.parseLong(s, 16));

            MemoryBlock b = currentProgram.getMemory().getBlock(a);
            if (b == null || !b.isExecute()) {
                w.printf("%s\tNONEXEC\t%s%n", a, b == null ? "unmapped" : b.getName());
                nonexec++;
                continue;
            }

            Function f = getFunctionAt(a);
            if (f != null) {
                w.printf("%s\tEXISTS\t%s%n", a, f.getName());
                existed++;
                continue;
            }

            Function containing = getFunctionContaining(a);
            if (containing != null) {
                // Mid-function target. Real for a tail-call table or a
                // hand-written jump target, but creating a function here would
                // split a body Ghidra already believes in. Report, don't guess.
                w.printf("%s\tINSIDE\t%s+0x%x%n", a, containing.getName(),
                        a.subtract(containing.getEntryPoint()));
                inside++;
                continue;
            }

            if (getInstructionAt(a) == null) {
                disassemble(a);
            }
            Function made = createFunction(a, null);
            if (made != null) {
                w.printf("%s\tCREATED\t%s%n", a, made.getName());
                created++;
            } else {
                w.printf("%s\tFAILED\t%s%n", a,
                        getInstructionAt(a) == null ? "no-disassembly" : "create-refused");
                failed++;
            }
        }

        w.printf("# created=%d existed=%d inside=%d failed=%d nonexec=%d%n",
                created, existed, inside, failed, nonexec);
        w.close();
        println(String.format(
                "SeedDataFunctions: created=%d existed=%d inside=%d failed=%d nonexec=%d -> %s",
                created, existed, inside, failed, nonexec, out));
    }
}
