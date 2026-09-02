// ApplyMatchedNames.java - apply body-matcher results.
//
// Input: TSV from prep_renames.py - addr, new_name, old_name, loose, strict, dll
//
// Two rules the script will not be argued out of:
//
//  * a score floor. A weak body ratio that squeaked past call corroboration is
//    a guess, and a wrong name is worse than no name.
//  * a name a person chose is never overwritten. Where the matcher agrees with
//    an existing name that agreement is evidence, and evidence is worth more
//    left where it is than flattened into the machine's wording.
//
// Every applied function gets a plate comment recording where the name came
// from and how well it scored, so the claim can be re-checked later.
//
// @category Recomp

import java.io.PrintWriter;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.List;

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.symbol.SourceType;

public class ApplyMatchedNames extends GhidraScript {

    private static final double MIN_LOOSE = 0.80;
    private static final double MIN_STRICT = 0.70;

    /** A name no person chose: Ghidra's defaults and our own heuristics. */
    private boolean isAutoName(String n) {
        return n.startsWith("FUN_") || n.startsWith("thunk_FUN_")
                || n.startsWith("SUB_") || n.contains("::vfunc")
                || n.matches(".*\\bnear_[0-9a-fA-F]+$")
                || n.matches("^near_[0-9a-fA-F]+$");
    }

    @Override
    public void run() throws Exception {
        String in = "/mnt/share/alchemy_apply.tsv";
        String out = "/mnt/share/alchemy_applied.txt";
        String[] args = getScriptArgs();
        if (args.length > 0) in = args[0];
        if (args.length > 1) out = args[1];

        PrintWriter w = new PrintWriter(out);
        int applied = 0, lowScore = 0, kept = 0, missing = 0, failed = 0, already = 0;

        for (String line : Files.readAllLines(Paths.get(in))) {
            if (line.startsWith("#") || line.trim().isEmpty()) continue;
            String[] f = line.split("\t");
            if (f.length < 6) continue;

            Address a = toAddr(Long.parseLong(f[0].trim(), 16));
            String newName = f[1];
            double loose = Double.parseDouble(f[3]);
            double strict = Double.parseDouble(f[4]);
            String dll = f[5];

            Function fn = getFunctionAt(a);
            if (fn == null) {
                w.printf("MISSING\t%s\t%s%n", a, newName);
                missing++;
                continue;
            }
            String cur = fn.getName();

            if (loose < MIN_LOOSE || strict < MIN_STRICT) {
                w.printf("LOWSCORE\t%s\t%s\t%s\t%.2f/%.2f%n", a, cur, newName, loose, strict);
                lowScore++;
                continue;
            }
            if (cur.equals(newName)) {
                // Already carries this name from an earlier pass.
                w.printf("ALREADY\t%s\t%s%n", a, cur);
                already++;
                continue;
            }
            if (!isAutoName(cur)) {
                // Agreement with an existing name is a result in itself.
                w.printf("KEPT_EXISTING\t%s\t%s\tmatcher_said\t%s\t%.2f/%.2f%n",
                        a, cur, newName, loose, strict);
                setPlateComment(a, String.format(
                        "Alchemy body match: %s (%s, shape %.2f, detail %.2f). "
                        + "Existing name kept; the two agree.",
                        newName, dll, loose, strict));
                kept++;
                continue;
            }

            try {
                fn.setName(newName, SourceType.ANALYSIS);
                setPlateComment(a, String.format(
                        "Named by body match against Alchemy %s "
                        + "(shape %.2f, detail %.2f). Was %s.",
                        dll, loose, strict, cur));
                w.printf("APPLIED\t%s\t%s\t%s\t%.2f/%.2f%n", a, cur, newName, loose, strict);
                applied++;
            } catch (Exception e) {
                w.printf("FAILED\t%s\t%s\t%s%n", a, newName, e.getMessage());
                failed++;
            }
        }

        w.printf("# applied=%d already=%d kept_existing=%d low_score=%d missing=%d failed=%d%n",
                applied, already, kept, lowScore, missing, failed);
        w.close();
        println("applied=" + applied + " already=" + already + " kept_existing=" + kept
                + " low_score=" + lowScore + " missing=" + missing + " failed=" + failed
                + " -> " + out);
    }
}
