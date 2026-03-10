package com.esg.model;

import java.util.HashMap;
import java.util.Map;
import java.util.Objects;

public class ESGNode {
    public enum NodeType {
        METHOD, STATE, DATA
    }

    private final String id;
    private final String label;
    private final NodeType type;
    private final String allocationSite; // Allocation site context
    private final Map<String, Object> attributes;

    public ESGNode(String id, String label, NodeType type) {
        this(id, label, type, null);
    }

    public ESGNode(String id, String label, NodeType type, String allocationSite) {
        this.id = id;
        this.label = label;
        this.type = type;
        this.allocationSite = allocationSite;
        this.attributes = new HashMap<>();
    }

    public String getId() { 
        return allocationSite != null ? id + "_" + allocationSite : id; 
    }
    
    public String getLabel() { return label; }
    public NodeType getType() { return type; }
    public String getAllocationSite() { return allocationSite; }
    public Map<String, Object> getAttributes() { return attributes; }

    @Override
    public boolean equals(Object o) {
        if (this == o) return true;
        if (o == null || getClass() != o.getClass()) return false;
        ESGNode esgNode = (ESGNode) o;
        return Objects.equals(id, esgNode.id);
    }

    @Override
    public int hashCode() {
        return Objects.hash(id);
    }
    
    @Override
    public String toString() {
        return type + ": " + label;
    }
}
