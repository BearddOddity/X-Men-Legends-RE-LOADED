// AuditVtableBounds.java - find vfuncN names that sit past the end of their
// class's vtable.
//
// WalkMsvcRtti.java numbered vtable slots by walking forward from the vtable
// and stopping at the first dword that is not a code pointer. Vtables are
// packed back to back in .rdata, so that walk runs straight out of one vtable
// and into the next one, attributing the next class's slots to the previous
// class.
//
// A vtable's real end is where the following vtable's complete-object-locator
// pointer sits: the MSVC layout is [COL][slot0][slot1]... so every vtable is
// preceded by exactly one COL pointer. Collect every such position and a
// vtable runs from V to the next one.
//
// Read-only by default. Pass "fix" as the first script argument to strip the
// bad names back to FUN_<addr>.
//
// @category Recomp

import java.io.PrintWriter;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.mem.Memory;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.symbol.SourceType;

public class AuditVtableBounds extends GhidraScript {

    private Memory mem;
    private Set<Long> initialized = new HashSet<>();

    private boolean isInInitialized(long v) {
        Address a;
        try { a = toAddr(v); } catch (Exception e) { return false; }
        MemoryBlock b = mem.getBlock(a);
        return b != null && b.isInitialized();
    }

    // The XBE loader does not always mark a block non-executable, so an
    // execute flag alone is not a usable "this is code" test. Use the blocks
    // that actually hold code, discovered once at startup.
    private Set<String> codeBlocks = new HashSet<>();

    private boolean isCodePtr(long v) {
        Address a;
        try { a = toAddr(v); } catch (Exception e) { return false; }
        MemoryBlock b = mem.getBlock(a);
        return b != null && b.isInitialized() && codeBlocks.contains(b.getName());
    }

    /** A complete object locator: signature 0, and +0x0C points at a
     *  type descriptor whose +8 is a ".?A" name. */
    private boolean isCol(long colVa) {
        try {
            if (mem.getInt(toAddr(colVa)) != 0) return false;
            long td = mem.getInt(toAddr(colVa + 0x0C)) & 0xFFFFFFFFL;
            if (!isInInitialized(td)) return false;
            byte[] tag = new byte[3];
            mem.getBytes(toAddr(td + 8), tag);
            return tag[0] == '.' && tag[1] == '?' && tag[2] == 'A';
        } catch (Exception e) {
            return false;
        }
    }

    private String classNameOfCol(long colVa) {
        try {
            long td = mem.getInt(toAddr(colVa + 0x0C)) & 0xFFFFFFFFL;
            StringBuilder sb = new StringBuilder();
            for (int i = 0; i < 192; i++) {
                byte b = mem.getByte(toAddr(td + 8 + i));
                if (b == 0) break;
                sb.append((char) (b & 0xFF));
            }
            return sb.toString();
        } catch (Exception e) {
            return "?";
        }
    }

    @Override
    public void run() throws Exception {
        mem = currentProgram.getMemory();
        String[] args = getScriptArgs();
        boolean fix = args.length > 0 && "fix".equals(args[0]);
        String out = args.length > 1 ? args[1] : "/mnt/share/vtable_audit.txt";

        // Every position P in an initialized, non-executable block whose dword
        // is a COL. The vtable then starts at P+4.
        TreeSet<Long> colSlots = new TreeSet<>();
        Map<Long, String> vtableClass = new HashMap<>();

        for (MemoryBlock b : mem.getBlocks()) {
            println(String.format("block %-12s %s-%s init=%b x=%b w=%b",
                    b.getName(), b.getStart(), b.getEnd(), b.isInitialized(),
                    b.isExecute(), b.isWrite()));
        }
        // A block holding at least one function entry is a code block.
        for (Function f : currentProgram.getFunctionManager().getFunctions(true)) {
            MemoryBlock b = mem.getBlock(f.getEntryPoint());
            if (b != null) codeBlocks.add(b.getName());
        }
        println("code blocks: " + codeBlocks);

        for (MemoryBlock b : mem.getBlocks()) {
            if (!b.isInitialized()) continue;
            long start = b.getStart().getOffset();
            long end = b.getEnd().getOffset() - 4;
            monitor.setMessage("scanning " + b.getName());
            for (long p = (start + 3) & ~3L; p <= end; p += 4) {
                if (monitor.isCancelled()) return;
                long v;
                try { v = mem.getInt(toAddr(p)) & 0xFFFFFFFFL; } catch (Exception e) { continue; }
                if (v == 0 || !isInInitialized(v)) continue;
                if (!isCol(v)) continue;
                // The dword right after a COL pointer must be a code pointer,
                // otherwise this is a COL reference that is not a vtable header.
                long first;
                try { first = mem.getInt(toAddr(p + 4)) & 0xFFFFFFFFL; } catch (Exception e) { continue; }
                if (!isCodePtr(first)) continue;
                colSlots.add(p);
                vtableClass.put(p + 4, classNameOfCol(v));
            }
        }
        println("vtable headers found: " + colSlots.size());

        PrintWriter w = new PrintWriter(out);
        w.printf("# vtable headers: %d%n", colSlots.size());

        // slot count per vtable = distance to the next vtable header, capped
        // where the run of code pointers stops.
        Map<Long, Integer> lengths = new HashMap<>();
        for (long p : colSlots) {
            long v = p + 4;
            Long nextHdr = colSlots.higher(p);
            long limit = nextHdr == null ? v + 4096 : nextHdr;
            int n = 0;
            for (long q = v; q < limit; q += 4) {
                long t;
                try { t = mem.getInt(toAddr(q)) & 0xFFFFFFFFL; } catch (Exception e) { break; }
                if (!isCodePtr(t)) break;
                n++;
            }
            lengths.put(v, n);
            w.printf("VTABLE\t%08x\t%d\t%s%n", v, n, vtableClass.get(v));
        }

        // Longest legitimate slot index seen per class name.
        Map<String, Integer> maxSlot = new HashMap<>();
        for (Map.Entry<Long, Integer> e : lengths.entrySet()) {
            String cls = vtableClass.get(e.getKey());
            if (cls == null) continue;
            String dm = demangleRtti(cls);
            Integer prev = maxSlot.get(dm);
            if (prev == null || e.getValue() > prev) maxSlot.put(dm, e.getValue());
        }

        int over = 0, ok = 0, unknown = 0, stripped = 0, failed = 0;
        List<String> sample = new ArrayList<>();
        for (Function f : currentProgram.getFunctionManager().getFunctions(true)) {
            if (monitor.isCancelled()) break;
            String n = f.getName();
            int i = n.indexOf("::vfunc");
            if (i < 0) continue;
            String cls = n.substring(0, i);
            int slot;
            try { slot = Integer.parseInt(n.substring(i + 7)); } catch (Exception e) { continue; }
            Integer max = maxSlot.get(cls);
            if (max == null) { unknown++; continue; }
            if (slot < max) { ok++; continue; }
            over++;
            if (sample.size() < 20) sample.add(n + " @ " + f.getEntryPoint() + " (class max " + max + ")");
            w.printf("OVERRUN\t%s\t%s\t%d\t%d%n", f.getEntryPoint(), cls, slot, max);
            if (fix) {
                // A clashing symbol must not take the whole pass down with it.
                try {
                    f.setName("FUN_" + f.getEntryPoint().toString().toLowerCase(),
                            SourceType.ANALYSIS);
                    stripped++;
                } catch (Exception ex) {
                    w.printf("STRIPFAIL\t%s\t%s\t%s%n", f.getEntryPoint(), n,
                            ex.getMessage());
                    failed++;
                }
            }
        }

        w.printf("# in_bounds=%d overrun=%d class_unknown=%d stripped=%d stripfail=%d%n",
                ok, over, unknown, stripped, failed);
        w.close();
        println("in_bounds=" + ok + " overrun=" + over + " class_unknown=" + unknown
                + " stripped=" + stripped + " stripfail=" + failed + " -> " + out);
        for (String s : sample) println("  " + s);
    }

    /** ".?AVigNode@Sg@Gap@@" -> "Gap::Sg::igNode", matching WalkMsvcRtti. */
    private String demangleRtti(String raw) {
        String s = raw;
        if (s.startsWith(".?AV") || s.startsWith(".?AU")) s = s.substring(4);
        if (s.endsWith("@@")) s = s.substring(0, s.length() - 2);
        String[] parts = s.split("@");
        StringBuilder sb = new StringBuilder();
        for (int i = parts.length - 1; i >= 0; i--) {
            if (parts[i].isEmpty()) continue;
            if (sb.length() > 0) sb.append("::");
            sb.append(parts[i]);
        }
        return sb.toString();
    }
}
