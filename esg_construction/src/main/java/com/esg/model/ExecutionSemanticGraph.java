package com.esg.model;

import java.util.HashMap;
import java.util.Map;
import java.util.HashSet;
import java.util.Set;

public class ExecutionSemanticGraph {
    private final Map<String, ESGNode> nodeIndex = new HashMap<>();
    private final Set<ESGEdge> edges = new HashSet<>();

    public void addNode(ESGNode node) {
        if (!nodeIndex.containsKey(node.getId())) {
            nodeIndex.put(node.getId(), node);
        }
    }
    
    public ESGNode getNode(String id) {
        return nodeIndex.get(id);
    }

    public void addEdge(ESGNode source, ESGNode target, ESGEdge.EdgeType type, String label) {
        edges.add(new ESGEdge(source, target, type, label));
    }

    public Set<ESGNode> getNodes() { return new HashSet<>(nodeIndex.values()); }
    public Set<ESGEdge> getEdges() { return edges; }

    public String toDot() {
        StringBuilder sb = new StringBuilder();
        sb.append("digraph ESG {\n");
        sb.append("  node [shape=box];\n");
        
        for (ESGNode node : nodeIndex.values()) {
            String color = "white";
            String shape = "box";
            switch(node.getType()) {
                case METHOD: color = "lightblue"; shape = "box"; break;
                case STATE: color = "lightyellow"; shape = "ellipse"; break;
                case DATA: color = "lightgreen"; shape = "parallelogram"; break;
            }
            sb.append(String.format("  \"%s\" [label=\"%s\", style=filled, fillcolor=%s, shape=%s];\n", 
                node.getId(), node.getLabel(), color, shape));
        }

        for (ESGEdge edge : edges) {
            String style = "solid";
            String color = "black";
            switch(edge.getType()) {
                case TEMPORAL: style = "solid"; color = "blue"; break;
                case STATE_TRANSITION: style = "dashed"; color = "orange"; break;
                case CAUSAL: style = "dotted"; color = "green"; break;
            }
            sb.append(String.format("  \"%s\" -> \"%s\" [label=\"%s\", style=%s, color=%s];\n",
                edge.getSource().getId(), edge.getTarget().getId(), edge.getLabel(), style, color));
        }

        sb.append("}\n");
        return sb.toString();
    }
}
