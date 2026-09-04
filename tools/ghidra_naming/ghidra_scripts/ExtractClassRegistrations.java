// ExtractClassRegistrations.java - read the game's own class registration table.
//
// Alchemy registers every class at startup through one function. Each call site
// is a run-once flag followed by a block of `push imm32` and a call, and the
// pushed values include a pointer to the class name as a plain C string and the
// class's size in bytes. The game therefore names its own classes, which is a
// better source than any SDK: no version gap, no fuzzy matching.
//
// Found by following a destructor the body matcher could not identify:
// 002766a0 <- 0027c080 <- pushed at 002894ac, whose block also pushes
// 00405f40 = "igBumpMapShader".
//
// Usage: ExtractClassRegistrations.java <registrar_va> [out]
//
// Output is a table, one row per call site: the pushed arguments classified as
// string / code / data / integer, so the argument order can be confirmed across
// every site before anything is renamed on the strength of it.
//
// @category Recomp

import java.io.PrintWriter;
import java.util.ArrayList;
import java.util.List;

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.symbol.Reference;
import ghidra.program.model.symbol.ReferenceIterator;

public class ExtractClassRegistrations extends GhidraScript {

    private static final int MAX_BACK = 80;   // bytes of push block to walk back

    private boolean inBlock(long v, String name) {
        Address a;
        try { a = toAddr(v); } catch (Exception e) { return false; }
        MemoryBlock b = currentProgram.getMemory().getBlock(a);
        return b != null && b.getName().equals(name);
    }

    /** A NUL terminated printable identifier at this address, or null. */
    private String cstringAt(long v) {
        Address a;
        try { a = toAddr(v); } catch (Exception e) { return null; }
        MemoryBlock b = currentProgram.getMemory().getBlock(a);
        if (b == null || !b.isInitialized()) return null;
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < 96; i++) {
            byte c;
            try { c = currentProgram.getMemory().getByte(a.add(i)); } catch (Exception e) { return null; }
            if (c == 0) break;
            if (c < 0x20 || c > 0x7e) return null;
            sb.append((char) c);
        }
        String s = sb.toString();
        if (s.length() < 3 || s.length() > 80) return null;
        if (!s.matches("[A-Za-z_][A-Za-z0-9_:]*")) return null;
        return s;
    }

    private String classify(long v) {
        String s = cstringAt(v);
        if (s != null) return "str:" + s;
        if (v < 0x10000) return "int:" + v;
        if (inBlock(v, ".text")) return "code:" + String.format("%08x", v);
        if (inBlock(v, ".data") || inBlock(v, ".rdata")) return "data:" + String.format("%08x", v);
        return "?:" + String.format("%08x", v);
    }

    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        if (args.length < 1) {
            println("need the registrar address");
            return;
        }
        Address registrar = toAddr(Long.parseLong(args[0].trim(), 16));
        String out = args.length > 1 ? args[1] : "/mnt/share/class_registrations.tsv";

        PrintWriter w = new PrintWriter(out);
        w.println("#site\tname\tsize\targs");

        int sites = 0, named = 0;
        ReferenceIterator it = currentProgram.getReferenceManager()
                .getReferencesTo(registrar);
        while (it.hasNext()) {
            if (monitor.isCancelled()) break;
            Reference ref = it.next();
            if (!ref.getReferenceType().isCall()) continue;
            Address site = ref.getFromAddress();
            sites++;

            // Walk back over the push block, collecting the pushed immediates
            // in the order they appear in the code.
            List<Long> pushed = new ArrayList<>();
            Instruction ins = getInstructionBefore(site);
            int walked = 0;
            while (ins != null && walked < MAX_BACK) {
                walked += ins.getLength();
                String m = ins.getMnemonicString();
                if (!m.equalsIgnoreCase("PUSH")) break;
                Object[] ops = ins.getOpObjects(0);
                if (ops.length == 1 && ops[0] instanceof ghidra.program.model.scalar.Scalar) {
                    pushed.add(0, ((ghidra.program.model.scalar.Scalar) ops[0])
                            .getUnsignedValue());
                } else {
                    break;   // a register push - not part of the literal block
                }
                ins = getInstructionBefore(ins.getAddress());
            }
            if (pushed.isEmpty()) continue;

            String name = null;
            long size = -1;
            StringBuilder cls = new StringBuilder();
            for (int i = 0; i < pushed.size(); i++) {
                long v = pushed.get(i);
                String c = classify(v);
                if (name == null && c.startsWith("str:")) {
                    name = c.substring(4);
                    // The size is conventionally the argument next to the name.
                    if (i + 1 < pushed.size() && pushed.get(i + 1) < 0x10000) {
                        size = pushed.get(i + 1);
                    } else if (i > 0 && pushed.get(i - 1) < 0x10000) {
                        size = pushed.get(i - 1);
                    }
                }
                if (cls.length() > 0) cls.append(" ");
                cls.append(c);
            }
            if (name != null) named++;
            w.printf("%s\t%s\t%s\t%s%n", site, name == null ? "-" : name,
                    size < 0 ? "-" : Long.toString(size), cls);
        }
        w.close();
        println("registration sites=" + sites + " with a class name=" + named
                + " -> " + out);
    }
}
