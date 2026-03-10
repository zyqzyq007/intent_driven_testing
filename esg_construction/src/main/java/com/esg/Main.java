package com.esg;

import com.esg.analyzer.ESGAnalyzer;
import com.esg.model.ESGEdge;
import com.esg.model.ESGNode;
import com.esg.model.ExecutionSemanticGraph;

import java.io.File;
import java.io.FileWriter;
import java.io.IOException;
import java.util.ArrayList;
import java.util.List;

public class Main {

    /**
     * Usage:
     *   java com.esg.Main [targetClassesDir] [outputDir]
     *
     * Defaults:
     *   targetClassesDir = /root/MyIntention/intent_driven_testing/data/raw/spark-master/target/classes
     *   outputDir        = /root/MyIntention/intent_driven_testing/data/processed/spark-master
     *
     * Produces two files in outputDir:
     *   spark_esg.dot   – Graphviz DOT (human-readable visualization)
     *   esg_graph.json  – Structured JSON (consumed by Python pipeline)
     */
    public static void main(String[] args) {
        String defaultRoot = "/root/MyIntention/intent_driven_testing";

        String targetPath = (args.length > 0)
                ? args[0]
                : defaultRoot + "/data/raw/spark-master/target/classes";

        String outputDir = (args.length > 1)
                ? args[1]
                : defaultRoot + "/data/processed/spark-master";

        // Build classpath: target classes + rt.jar/jce.jar if available
        String javaHome = System.getenv("JAVA_HOME");
        String classPath = targetPath;
        if (javaHome != null) {
            String rtJar  = javaHome + File.separator + "lib" + File.separator + "rt.jar";
            String jceJar = javaHome + File.separator + "lib" + File.separator + "jce.jar";
            if (new File(rtJar).exists())  classPath += File.pathSeparator + rtJar;
            if (new File(jceJar).exists()) classPath += File.pathSeparator + jceJar;
        }

        System.out.println("[ESG] Target   : " + targetPath);
        System.out.println("[ESG] ClassPath: " + classPath);
        System.out.println("[ESG] OutputDir: " + outputDir);

        new File(outputDir).mkdirs();

        ESGAnalyzer analyzer = new ESGAnalyzer(targetPath, classPath);
        ExecutionSemanticGraph esg = analyzer.analyze();

        if (esg == null) {
            System.err.println("[ESG] Analysis returned null – aborting.");
            System.exit(1);
        }

        System.out.println("[ESG] Graph built: "
                + esg.getNodes().size() + " nodes, "
                + esg.getEdges().size() + " edges.");

        writeDot(esg, outputDir + File.separator + "spark_esg.dot");
        writeJson(esg, outputDir + File.separator + "esg_graph.json");

        System.out.println("[ESG] Done.");
    }

    // -----------------------------------------------------------------------
    private static void writeDot(ExecutionSemanticGraph esg, String path) {
        try (FileWriter w = new FileWriter(path)) {
            w.write(esg.toDot());
            System.out.println("[ESG] DOT  → " + path);
        } catch (IOException e) {
            System.err.println("[ESG] Failed to write DOT: " + e.getMessage());
        }
    }

    // -----------------------------------------------------------------------
    // JSON Schema:
    // {
    //   "nodes": [ { "id", "label", "type", "allocation_site"? } ],
    //   "edges": [ { "source", "target", "edge_type", "label" } ]
    // }
    private static void writeJson(ExecutionSemanticGraph esg, String path) {
        StringBuilder sb = new StringBuilder();
        sb.append("{\n");

        // nodes
        sb.append("  \"nodes\": [\n");
        List<ESGNode> nodes = new ArrayList<>(esg.getNodes());
        for (int i = 0; i < nodes.size(); i++) {
            ESGNode n = nodes.get(i);
            sb.append("    {");
            sb.append("\"id\": ").append(js(n.getId())).append(", ");
            sb.append("\"label\": ").append(js(n.getLabel())).append(", ");
            sb.append("\"type\": ").append(js(n.getType().name()));
            if (n.getAllocationSite() != null) {
                sb.append(", \"allocation_site\": ").append(js(n.getAllocationSite()));
            }
            sb.append("}");
            if (i < nodes.size() - 1) sb.append(",");
            sb.append("\n");
        }
        sb.append("  ],\n");

        // edges
        sb.append("  \"edges\": [\n");
        List<ESGEdge> edges = new ArrayList<>(esg.getEdges());
        for (int i = 0; i < edges.size(); i++) {
            ESGEdge e = edges.get(i);
            sb.append("    {");
            sb.append("\"source\": ").append(js(e.getSource().getId())).append(", ");
            sb.append("\"target\": ").append(js(e.getTarget().getId())).append(", ");
            sb.append("\"edge_type\": ").append(js(e.getType().name())).append(", ");
            sb.append("\"label\": ").append(js(e.getLabel()));
            sb.append("}");
            if (i < edges.size() - 1) sb.append(",");
            sb.append("\n");
        }
        sb.append("  ]\n");
        sb.append("}\n");

        try (FileWriter w = new FileWriter(path)) {
            w.write(sb.toString());
            System.out.println("[ESG] JSON → " + path);
        } catch (IOException e) {
            System.err.println("[ESG] Failed to write JSON: " + e.getMessage());
        }
    }

    /** Minimal JSON string escaping. */
    private static String js(String s) {
        if (s == null) return "null";
        return "\"" + s.replace("\\", "\\\\")
                       .replace("\"", "\\\"")
                       .replace("\n", "\\n")
                       .replace("\r", "\\r")
                       .replace("\t", "\\t")
             + "\"";
    }
}
