// ApplyRegistrationNames.java - name functions and globals from the game's own
// class registration table.
//
// Each of the 698 registration sites pushes eleven arguments in a fixed order:
//
//   code:A code:B int:SIZE str:NAME code:C code:D code:E data:META int:0
//
// The roles were established by reading one example of each, not by assuming:
//
//   A  registerFields  - populates this class's metaobject field array
//   B  the deleting destructor wrapper, which calls the real destructor
//   C  getClassMeta    - returns this class's _Meta global
//   D  the PARENT class's getClassMeta (so it names an inheritance edge,
//      not a function of this class)
//   E  the PARENT class's arkRegister block
//
// A and B are present at only 429 of the 698 sites - the instantiable classes.
//
// D and E therefore get no names here: they belong to the parent and are named
// when the parent's own row is processed. They are written to the hierarchy
// file instead.
//
// @category Recomp

import java.io.PrintWriter;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.symbol.Reference;
import ghidra.program.model.symbol.ReferenceIterator;
import ghidra.program.model.symbol.SourceType;
import ghidra.program.model.symbol.Symbol;
import ghidra.program.model.symbol.SymbolTable;

public class ApplyRegistrationNames extends GhidraScript {

    private int applied = 0, kept = 0, failed = 0;

    private boolean isAutoName(String n) {
        return n.startsWith("FUN_") || n.startsWith("thunk_FUN_")
                || n.startsWith("SUB_") || n.startsWith("DAT_")
                || n.startsWith("PTR_") || n.startsWith("UNK_")
                || n.contains("::vfunc")
                || n.matches("^(Gap::)?near_[0-9a-fA-F]+$")
                || n.endsWith("::registered_slot0");
    }

    private void nameFunc(PrintWriter w, long va, String name, String role) {
        Address a;
        try { a = toAddr(va); } catch (Exception e) { return; }
        Function f = getFunctionAt(a);
        if (f == null) {
            w.printf("NOFUNC\t%08x\t%s\t%s%n", va, name, role);
            return;
        }
        if (!isAutoName(f.getName())) {
            w.printf("KEPT\t%08x\t%s\twould_be\t%s%n", va, f.getName(), name);
            kept++;
            return;
        }
        try {
            f.setName(name, SourceType.ANALYSIS);
            setPlateComment(a, "From the game's class registration table (slot "
                    + role + ").");
            w.printf("FUNC\t%08x\t%s%n", va, name);
            applied++;
        } catch (Exception e) {
            w.printf("FAILED\t%08x\t%s\t%s%n", va, name, e.getMessage());
            failed++;
        }
    }

    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        String table = args.length > 0 ? args[0] : "/mnt/share/class_registrations.tsv";
        String out = args.length > 1 ? args[1] : "/mnt/share/registration_names.txt";
        String hier = args.length > 2 ? args[2] : "/mnt/share/class_hierarchy.tsv";

        List<String[]> rows = new ArrayList<>();
        for (String line : Files.readAllLines(Paths.get(table))) {
            if (line.startsWith("#")) continue;
            String[] f = line.split("\t");
            if (f.length >= 4 && !f[1].equals("-")) rows.add(f);
        }
        println("rows: " + rows.size());

        // getClassMeta (slot C) -> class name, so slot D can be resolved to a
        // parent class rather than left as a bare address.
        Map<Long, String> metaAccessorOwner = new HashMap<>();
        Map<String, String[]> parsed = new HashMap<>();
        for (String[] r : rows) {
            String[] toks = r[3].split(" ");
            int ni = -1;
            for (int i = 0; i < toks.length; i++) if (toks[i].startsWith("str:")) { ni = i; break; }
            if (ni < 0) continue;
            List<String> pre = new ArrayList<>(), post = new ArrayList<>();
            for (int i = 0; i < ni - 1; i++) if (toks[i].startsWith("code:")) pre.add(toks[i].substring(5));
            for (int i = ni + 1; i < toks.length; i++) if (toks[i].startsWith("code:")) post.add(toks[i].substring(5));
            String meta = null;
            for (int i = ni + 1; i < toks.length; i++) if (toks[i].startsWith("data:")) meta = toks[i].substring(5);
            if (post.size() < 3) continue;
            String a = pre.size() >= 2 ? pre.get(pre.size() - 2) : null;
            String b = pre.size() >= 2 ? pre.get(pre.size() - 1) : null;
            parsed.put(r[1], new String[]{a, b, post.get(0), post.get(1), post.get(2), meta, r[2]});
            metaAccessorOwner.put(Long.parseLong(post.get(0), 16), r[1]);
        }

        PrintWriter w = new PrintWriter(out);
        SymbolTable st = currentProgram.getSymbolTable();
        int globals = 0;

        for (Map.Entry<String, String[]> e : parsed.entrySet()) {
            if (monitor.isCancelled()) break;
            String cls = e.getKey();
            String[] v = e.getValue();

            if (v[2] != null) nameFunc(w, Long.parseLong(v[2], 16), cls + "::getClassMeta", "C");
            if (v[0] != null) nameFunc(w, Long.parseLong(v[0], 16), cls + "::registerFields", "A");
            if (v[1] != null) {
                long bva = Long.parseLong(v[1], 16);
                nameFunc(w, bva, cls + "::scalar_deleting_dtor", "B");
                // The real destructor is the single function the wrapper calls.
                Function bf = getFunctionAt(toAddr(bva));
                if (bf != null) {
                    // The call references out of the wrapper's body.
                    List<Address> callees = new ArrayList<>();
                    ReferenceIterator ri = currentProgram.getReferenceManager()
                            .getReferenceIterator(bf.getBody().getMinAddress());
                    while (ri.hasNext()) {
                        Reference r = ri.next();
                        if (r.getFromAddress().compareTo(bf.getBody().getMaxAddress()) > 0) break;
                        if (!r.getReferenceType().isCall()) continue;
                        Function t = getFunctionAt(r.getToAddress());
                        if (t != null && !t.equals(bf)) callees.add(r.getToAddress());
                    }
                    if (callees.size() == 1) {
                        nameFunc(w, callees.get(0).getOffset(), cls + "::dtor", "B-callee");
                    }
                }
            }

            if (v[5] != null) {
                Address ma = toAddr(Long.parseLong(v[5], 16));
                Symbol s = st.getPrimarySymbol(ma);
                if (s == null || isAutoName(s.getName())) {
                    try {
                        createLabel(ma, cls + "_Meta", true, SourceType.ANALYSIS);
                        w.printf("GLOBAL\t%s\t%s_Meta%n", ma, cls);
                        globals++;
                    } catch (Exception ex) {
                        w.printf("FAILED\t%s\t%s_Meta\t%s%n", ma, cls, ex.getMessage());
                    }
                }
            }
        }

        // Inheritance: slot D is the parent's getClassMeta.
        PrintWriter h = new PrintWriter(hier);
        h.println("#class\tsize\tparent\tinstantiable");
        int edges = 0;
        for (Map.Entry<String, String[]> e : parsed.entrySet()) {
            String[] v = e.getValue();
            String parent = v[3] == null ? null
                    : metaAccessorOwner.get(Long.parseLong(v[3], 16));
            if (parent != null && !parent.equals(e.getKey())) edges++;
            h.printf("%s\t%s\t%s\t%s%n", e.getKey(), v[6],
                    parent == null ? "-" : parent, v[0] != null ? "yes" : "no");
        }
        h.close();

        w.printf("# functions=%d globals=%d kept=%d failed=%d parent_edges=%d%n",
                applied, globals, kept, failed, edges);
        w.close();
        println("functions=" + applied + " globals=" + globals + " kept=" + kept
                + " failed=" + failed + " parent_edges=" + edges);
    }
}
