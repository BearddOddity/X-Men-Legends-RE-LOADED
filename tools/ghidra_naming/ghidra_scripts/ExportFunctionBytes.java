// ExportFunctionBytes.java - dump every function's entry, size and body bytes.
//
// The body matcher disassembles both sides with the same capstone code path so
// that normalisation cannot drift between them. That means Ghidra's job here is
// only to say where the functions are and hand over the bytes.
//
// Output: TSV, one row per function - address, size, name, hex bytes.
//
// @category Recomp

import java.io.PrintWriter;

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;

public class ExportFunctionBytes extends GhidraScript {

    private static final int MAX_BYTES = 4096;

    @Override
    public void run() throws Exception {
        String out = "/mnt/share/game_func_bytes.tsv";
        String[] args = getScriptArgs();
        if (args.length > 0) out = args[0];

        PrintWriter w = new PrintWriter(out);
        w.println("#addr\tsize\tname\thex");

        int n = 0, empty = 0;
        StringBuilder sb = new StringBuilder();
        for (Function f : currentProgram.getFunctionManager().getFunctions(true)) {
            if (monitor.isCancelled()) break;
            Address min = f.getEntryPoint();
            long len = f.getBody().getNumAddresses();
            if (len <= 0) { empty++; continue; }
            if (len > MAX_BYTES) len = MAX_BYTES;

            byte[] buf = new byte[(int) len];
            int got;
            try {
                got = currentProgram.getMemory().getBytes(min, buf);
            } catch (Exception e) {
                empty++;
                continue;
            }
            if (got <= 0) { empty++; continue; }

            sb.setLength(0);
            for (int i = 0; i < got; i++) sb.append(String.format("%02x", buf[i]));
            w.printf("%s\t%d\t%s\t%s%n", min, got, f.getName(), sb);
            n++;
        }
        w.close();
        println("exported " + n + " functions (" + empty + " skipped) -> " + out);
    }
}
