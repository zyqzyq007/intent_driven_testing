package com.esg.model;

public class ESGEdge {
    public enum EdgeType {
        TEMPORAL, STATE_TRANSITION, CAUSAL
    }

    private final ESGNode source;
    private final ESGNode target;
    private final EdgeType type;
    private final String label;

    public ESGEdge(ESGNode source, ESGNode target, EdgeType type, String label) {
        this.source = source;
        this.target = target;
        this.type = type;
        this.label = label;
    }

    public ESGNode getSource() { return source; }
    public ESGNode getTarget() { return target; }
    public EdgeType getType() { return type; }
    public String getLabel() { return label; }
    
    @Override
    public String toString() {
        return source.getLabel() + " --[" + type + "]--> " + target.getLabel();
    }
}
