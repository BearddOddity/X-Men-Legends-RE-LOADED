// NameByRegistration.java - name functions from the game's own class
// registration table.
//
// Each registration site pushes a class name as a plain C string alongside the
// class's function pointers, so a function reachable from one of those pointers
// can be attributed to a class with no SDK and no fuzzy matching involved.
//
// Alchemy destructors sit one call behind the pointer that is registered: the
// registered function is the deleting wrapper, which calls the real destructor.
// So a target is named when its single caller appears as a code argument at a
// registration site.
//
// Input:  the table from ExtractClassRegistrations.java, and a list of
//         addresses to name.
// Output: what was named and on what evidence.
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

public class NameByRegistration extends GhidraScript {

    private boolean isAutoName(String n) {
        return n.startsWith("FUN_") || n.startsWith("thunk_FUN_")
                || n.startsWith("SUB_") || n.contains("::vfunc")
                || n.matches("^(Gap::)?near_[0-9a-fA-F]+$");
    }

    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        String table = args.length > 0 ? args[0] : "/mnt/share/class_registrations.tsv";
        String list = args.length > 1 ? args[1] : "/mnt/share/dtor_family.txt";
        String out = args.length > 2 ? args[2] : "/mnt/share/named_by_registration.txt";

        // code pointer -> class name, from the registration table
        Map<Long, String> owner = new HashMap<>();
        for (String line : Files.readAllLines(Paths.get(table))) {
            if (line.startsWith("#")) continue;
            String[] f = line.split("\t");
            if (f.length < 4 || f[1].equals("-")) continue;
            for (String tok : f[3].split(" ")) {
                if (tok.startsWith("code:")) {
                    owner.put(Long.parseLong(tok.substring(5), 16), f[1]);
                }
            }
        }
        println("registered code pointers: " + owner.size());

        PrintWriter w = new PrintWriter(out);
        int named = 0, noOwner = 0, skipped = 0;

        for (String line : Files.readAllLines(Paths.get(list))) {
            String s = line.trim();
            if (s.isEmpty() || s.startsWith("#")) continue;
            Address a = toAddr(Long.parseLong(s, 16));
            Function fn = getFunctionAt(a);
            if (fn == null) { w.printf("MISSING\t%s%n", a); continue; }

            // Who calls this? For a destructor that is the deleting wrapper.
            List<Address> callers = new ArrayList<>();
            ReferenceIterator it = currentProgram.getReferenceManager().getReferencesTo(a);
            while (it.hasNext()) {
                Reference r = it.next();
                if (!r.getReferenceType().isCall()) continue;
                Function c = getFunctionContaining(r.getFromAddress());
                if (c != null) callers.add(c.getEntryPoint());
            }

            String cls = null;
            Address wrapper = null;
            for (Address c : callers) {
                String o = owner.get(c.getOffset());
                if (o != null) { cls = o; wrapper = c; break; }
            }
            // The function may itself be registered.
            if (cls == null && owner.containsKey(a.getOffset())) {
                cls = owner.get(a.getOffset());
            }

            if (cls == null) {
                w.printf("NO_OWNER\t%s\t%s\tcallers=%d%n", a, fn.getName(), callers.size());
                noOwner++;
                continue;
            }

            String newName = cls + "::dtor";
            if (!isAutoName(fn.getName())) {
                w.printf("KEPT\t%s\t%s\twould_be\t%s%n", a, fn.getName(), newName);
                skipped++;
                continue;
            }
            try {
                fn.setName(newName, SourceType.ANALYSIS);
                setPlateComment(a, String.format(
                        "Named from the game's own class registration table: the "
                        + "caller %s is a registered function pointer for class %s. "
                        + "No SDK or body matching involved.",
                        wrapper == null ? "(self)" : wrapper.toString(), cls));
                if (wrapper != null) {
                    Function wf = getFunctionAt(wrapper);
                    if (wf != null && isAutoName(wf.getName())) {
                        wf.setName(cls + "::scalar_deleting_dtor", SourceType.ANALYSIS);
                    }
                }
                w.printf("NAMED\t%s\t%s\tvia\t%s%n", a, newName, wrapper);
                named++;
            } catch (Exception e) {
                w.printf("FAILED\t%s\t%s\t%s%n", a, newName, e.getMessage());
            }
        }
        w.printf("# named=%d no_owner=%d kept=%d%n", named, noOwner, skipped);
        w.close();
        println("named=" + named + " no_owner=" + noOwner + " kept=" + skipped
                + " -> " + out);
    }
}
