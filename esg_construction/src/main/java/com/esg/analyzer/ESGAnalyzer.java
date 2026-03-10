package com.esg.analyzer;

import com.esg.model.ESGEdge;
import com.esg.model.ESGNode;
import com.esg.model.ExecutionSemanticGraph;
import soot.*;
import soot.jimple.FieldRef;
import soot.jimple.Stmt;
import soot.jimple.IntConstant;
import soot.jimple.toolkits.callgraph.CallGraph;
import soot.jimple.toolkits.callgraph.Edge;
import soot.options.Options;

import java.util.*;

import soot.toolkits.graph.UnitGraph;
import soot.toolkits.graph.ExceptionalUnitGraph;

import soot.toolkits.graph.MHGDominatorsFinder;

import soot.toolkits.scalar.SimpleLocalDefs;
import soot.toolkits.scalar.SimpleLocalUses;
import soot.toolkits.scalar.UnitValueBoxPair;

import soot.toolkits.graph.MHGPostDominatorsFinder;

import soot.jimple.NewExpr;

public class ESGAnalyzer {
    private final String targetPath;
    private final String classPath;
    private ExecutionSemanticGraph esg;

    public ESGAnalyzer(String targetPath, String classPath) {
        this.targetPath = targetPath;
        this.classPath = classPath;
    }

    public ExecutionSemanticGraph analyze() {
        setupSoot();
        
        // Add a SceneTransformer to wjtp pack
        PackManager.v().getPack("wjtp").add(new Transform("wjtp.esg", new SceneTransformer() {
            @Override
            protected void internalTransform(String phaseName, Map<String, String> options) {
                esg = new ExecutionSemanticGraph();
                
                System.out.println("Building Temporal Layer...");
                buildTemporalLayer(esg);

                System.out.println("Building State Layer...");
                buildStateLayer(esg);

                System.out.println("Building Data Layer...");
                buildDataLayer(esg);
            }
        }));

        // Run Soot packs
        try {
            PackManager.v().runPacks();
        } catch (Exception e) {
            System.err.println("Error running Soot packs: " + e.getMessage());
            e.printStackTrace();
        }

        return esg;
    }

    private void setupSoot() {
        G.reset();
        Options.v().set_prepend_classpath(true);
        Options.v().set_allow_phantom_refs(true);
        Options.v().set_soot_classpath(classPath);
        
        // Use full-resolver for better precision, but might be slow
        // Options.v().set_full_resolver(true);
        
        Options.v().set_whole_program(true);
        
        // Analyze directories (process-dir) instead of a single class
        List<String> processDirs = new ArrayList<>();
        processDirs.add(targetPath);
        Options.v().set_process_dir(processDirs);
        
        // Keep line numbers
        Options.v().set_keep_line_number(true);
        
        // No output (we just want analysis)
        Options.v().set_output_format(Options.output_format_none);
        
        // Enable Call Graph construction (Spark or CHA)
        Options.v().setPhaseOption("cg.spark", "on");
        Options.v().setPhaseOption("cg.spark", "verbose:true");
        
        System.out.println("Soot setup complete. Target path: " + targetPath);
        
        Scene.v().loadNecessaryClasses();
    }

    private void buildTemporalLayer(ExecutionSemanticGraph esg) {
        // Use CH for now to ensure edges are generated even if Spark analysis is incomplete
        CallGraph cg = Scene.v().getCallGraph();
        
        // Iterate over a copy to avoid concurrent modification if we modify scene (unlikely here but safe)
        List<SootClass> appClasses = new ArrayList<>(Scene.v().getApplicationClasses());
        
        for (SootClass sc : appClasses) {
            // Skip library classes if any slipped in
            if (sc.isLibraryClass()) continue;

            for (SootMethod method : sc.getMethods()) {
                if (!method.hasActiveBody()) continue;

                ESGNode methodNode = getOrCreateMethodNode(esg, method);

                // 1. Inter-procedural: Call Graph Edges (May-Call)
                Iterator<Edge> edges = cg.edgesOutOf(method);
                while (edges.hasNext()) {
                    Edge edge = edges.next();
                    SootMethod targetMethod = edge.tgt();
                    
                    if (targetMethod.getDeclaringClass().isApplicationClass()) {
                        ESGNode targetNode = getOrCreateMethodNode(esg, targetMethod);
                        esg.addEdge(methodNode, targetNode, ESGEdge.EdgeType.TEMPORAL, "may_call");
                    }
                }

                // 2. Intra-procedural: True CFG Traversal
                Body body = method.getActiveBody();
                UnitGraph unitGraph = new soot.toolkits.graph.ExceptionalUnitGraph(body); // Use ExceptionalUnitGraph to include exception paths
                
                // We use a worklist algorithm to traverse the CFG
                Queue<Unit> worklist = new LinkedList<>();
                Set<Unit> visited = new HashSet<>();
                
                // Start from heads (entry points of the method)
                worklist.addAll(unitGraph.getHeads());
                
                // Map to track the "last call node" on a path reaching this unit
                // In a full analysis this would be a dataflow set, here we simplify
                Map<Unit, Set<ESGNode>> reachingCalls = new HashMap<>();
                
                while (!worklist.isEmpty()) {
                    Unit currentUnit = worklist.poll();
                    visited.add(currentUnit); // Just to track we processed it, but we might revisit if state changes
                    
                    Set<ESGNode> currentCallNodes = new HashSet<>();
                    
                    // Check if current unit is a method call
                    Stmt stmt = (Stmt) currentUnit;
                    if (stmt.containsInvokeExpr()) {
                         SootMethod invokedMethod = stmt.getInvokeExpr().getMethod();
                         if (invokedMethod.getDeclaringClass().isApplicationClass()) {
                             ESGNode currentCallNode = getOrCreateMethodNode(esg, invokedMethod);
                             currentCallNodes.add(currentCallNode);
                             
                             // Link from predecessors' reaching calls
                             for (Unit pred : unitGraph.getPredsOf(currentUnit)) {
                                 Set<ESGNode> prevCalls = reachingCalls.get(pred);
                                 if (prevCalls != null) {
                                     for (ESGNode prevCall : prevCalls) {
                                         if (!prevCall.equals(currentCallNode)) {
                                             esg.addEdge(prevCall, currentCallNode, ESGEdge.EdgeType.TEMPORAL, "follows_in_" + method.getName());
                                         }
                                     }
                                 }
                             }
                         }
                    }
                    
                    // Propagate reaching info to successors
                    Set<ESGNode> nodesToPropagate = new HashSet<>();
                    if (!currentCallNodes.isEmpty()) {
                        nodesToPropagate.addAll(currentCallNodes);
                    } else {
                        // If not a call, propagate all reaching calls from preds
                        for (Unit pred : unitGraph.getPredsOf(currentUnit)) {
                            Set<ESGNode> prevCalls = reachingCalls.get(pred);
                            if (prevCalls != null) {
                                nodesToPropagate.addAll(prevCalls);
                            }
                        }
                    }
                    
                    // Check if reaching state changed
                    Set<ESGNode> existingState = reachingCalls.get(currentUnit);
                    if (existingState == null || !existingState.equals(nodesToPropagate)) {
                         reachingCalls.put(currentUnit, nodesToPropagate);
                         // If changed, add successors to worklist
                         worklist.addAll(unitGraph.getSuccsOf(currentUnit));
                    }
                }
            }
        }
    }

    private void buildStateLayer(ExecutionSemanticGraph esg) {
        List<SootClass> appClasses = new ArrayList<>(Scene.v().getApplicationClasses());
        for (SootClass sc : appClasses) {
            // 1. Identify Enum Fields (Abstract Lifecycle States)
            for (SootField field : sc.getFields()) {
                 if (field.getType() instanceof RefType) {
                     SootClass fieldClass = ((RefType) field.getType()).getSootClass();
                     if (fieldClass.hasSuperclass() && fieldClass.getSuperclass().getName().equals("java.lang.Enum")) {
                         // Create Nodes
                         Map<String, ESGNode> enumNodes = new HashMap<>();
                         for (SootField enumConst : fieldClass.getFields()) {
                             if (enumConst.isStatic() && enumConst.getType().equals(field.getType())) {
                                 String nodeId = sc.getName() + "." + field.getName() + "_" + enumConst.getName();
                                 ESGNode stateNode = new ESGNode(nodeId, field.getName() + "=" + enumConst.getName(), ESGNode.NodeType.STATE);
                                 esg.addNode(stateNode);
                                 enumNodes.put(enumConst.getName(), stateNode);
                             }
                         }
                         analyzeEnumTransitions(esg, sc, field, fieldClass, enumNodes);
                     }
                 }
                 // 2. Identify Boolean Fields (Two-State Lifecycle)
                 else if (field.getType() instanceof BooleanType) {
                     Map<String, ESGNode> boolNodes = new HashMap<>();
                     
                     String trueNodeId = sc.getName() + "." + field.getName() + "_TRUE";
                     ESGNode trueNode = new ESGNode(trueNodeId, field.getName() + "=TRUE", ESGNode.NodeType.STATE);
                     esg.addNode(trueNode);
                     boolNodes.put("true", trueNode);
                     
                     String falseNodeId = sc.getName() + "." + field.getName() + "_FALSE";
                     ESGNode falseNode = new ESGNode(falseNodeId, field.getName() + "=FALSE", ESGNode.NodeType.STATE);
                     esg.addNode(falseNode);
                     boolNodes.put("false", falseNode);
                     
                     analyzeBooleanTransitions(esg, sc, field, boolNodes);
                 }
            }
        }
    }
    
    private void analyzeEnumTransitions(ExecutionSemanticGraph esg, SootClass sc, SootField field, SootClass enumClass, Map<String, ESGNode> enumNodes) {
        for (SootMethod method : sc.getMethods()) {
            if (!method.hasActiveBody()) continue;
            Body body = method.getActiveBody();
            UnitGraph graph = new soot.toolkits.graph.ExceptionalUnitGraph(body);
            SimpleLocalDefs localDefs = null;
            
            for (Unit u : body.getUnits()) {
                if (u instanceof soot.jimple.AssignStmt) {
                    soot.jimple.AssignStmt assign = (soot.jimple.AssignStmt) u;
                    if (assign.getLeftOp() instanceof FieldRef) {
                        FieldRef leftRef = (FieldRef) assign.getLeftOp();
                        if (leftRef.getField().equals(field)) {
                             // Found a State Update: Post-State
                             Value rightOp = assign.getRightOp();
                             SootField assignedEnumConst = null;

                             // Case 1: Direct FieldRef (unlikely in Jimple but possible)
                             if (rightOp instanceof FieldRef) {
                                 assignedEnumConst = ((FieldRef) rightOp).getField();
                             } 
                             // Case 2: Local variable (common case)
                             else if (rightOp instanceof Local) {
                                 if (localDefs == null) {
                                     localDefs = new SimpleLocalDefs(graph);
                                 }
                                 // Trace back to definition
                                 List<Unit> defs = localDefs.getDefsOfAt((Local) rightOp, u);
                                 for (Unit defUnit : defs) {
                                     if (defUnit instanceof soot.jimple.AssignStmt) {
                                         Value defRightOp = ((soot.jimple.AssignStmt) defUnit).getRightOp();
                                         if (defRightOp instanceof FieldRef) {
                                             assignedEnumConst = ((FieldRef) defRightOp).getField();
                                             break; // Found it
                                         }
                                     }
                                 }
                             }

                             if (assignedEnumConst != null) {
                                 if (assignedEnumConst.getDeclaringClass().equals(enumClass)) {
                                     ESGNode postStateNode = enumNodes.get(assignedEnumConst.getName());
                                     if (postStateNode != null) {
                                         ESGNode methodNode = getOrCreateMethodNode(esg, method);
                                         
                                         // 1. Link Method -> PostState
                                         esg.addEdge(methodNode, postStateNode, ESGEdge.EdgeType.STATE_TRANSITION, "transitions_to");
                                         
                                         // 2. Try to find Pre-State (Guard Condition)
                                         findGuardedPreState(esg, graph, u, field, enumClass, enumNodes, methodNode);
                                     }
                                 }
                             }
                        }
                    }
                }
            }
        }
    }
    
    private void analyzeBooleanTransitions(ExecutionSemanticGraph esg, SootClass sc, SootField field, Map<String, ESGNode> boolNodes) {
        for (SootMethod method : sc.getMethods()) {
            if (!method.hasActiveBody()) continue;
            Body body = method.getActiveBody();
            UnitGraph graph = new soot.toolkits.graph.ExceptionalUnitGraph(body);
            SimpleLocalDefs localDefs = null;
            
            for (Unit u : body.getUnits()) {
                if (u instanceof soot.jimple.AssignStmt) {
                    soot.jimple.AssignStmt assign = (soot.jimple.AssignStmt) u;
                    if (assign.getLeftOp() instanceof FieldRef) {
                        FieldRef leftRef = (FieldRef) assign.getLeftOp();
                        if (leftRef.getField().equals(field)) {
                             Value rightOp = assign.getRightOp();
                             String assignedValue = null;
                             
                             if (rightOp instanceof IntConstant) {
                                 assignedValue = (((IntConstant) rightOp).value == 1) ? "true" : "false";
                             } else if (rightOp instanceof Local) {
                                 if (localDefs == null) localDefs = new SimpleLocalDefs(graph);
                                 List<Unit> defs = localDefs.getDefsOfAt((Local) rightOp, u);
                                 for (Unit defUnit : defs) {
                                     if (defUnit instanceof soot.jimple.AssignStmt) {
                                         Value defRight = ((soot.jimple.AssignStmt) defUnit).getRightOp();
                                         if (defRight instanceof IntConstant) {
                                             assignedValue = (((IntConstant) defRight).value == 1) ? "true" : "false";
                                             break;
                                         }
                                     }
                                 }
                             }
                             
                             if (assignedValue != null) {
                                 ESGNode postStateNode = boolNodes.get(assignedValue);
                                 if (postStateNode != null) {
                                     ESGNode methodNode = getOrCreateMethodNode(esg, method);
                                     esg.addEdge(methodNode, postStateNode, ESGEdge.EdgeType.STATE_TRANSITION, "transitions_to");
                                     findGuardedPreStateBoolean(esg, graph, u, field, boolNodes, methodNode);
                                 }
                             }
                        }
                    }
                }
            }
        }
    }

    private void findGuardedPreStateBoolean(ExecutionSemanticGraph esg, UnitGraph graph, Unit updateUnit, SootField field, Map<String, ESGNode> boolNodes, ESGNode methodNode) {
        MHGPostDominatorsFinder<Unit> postDomFinder = new MHGPostDominatorsFinder<>(graph);
        SimpleLocalDefs localDefs = new SimpleLocalDefs(graph);
        
        for (Unit u : graph) {
            if (u instanceof soot.jimple.IfStmt) {
                soot.jimple.IfStmt ifStmt = (soot.jimple.IfStmt) u;
                Value condition = ifStmt.getCondition();
                
                if (!(condition instanceof soot.jimple.EqExpr || condition instanceof soot.jimple.NeExpr)) continue;
                
                soot.jimple.BinopExpr expr = (soot.jimple.BinopExpr) condition;
                Value op1 = expr.getOp1();
                Value op2 = expr.getOp2();
                
                boolean checksField = false;
                boolean checkIsTrue = true;
                
                Value tracedValue = null;
                int constVal = -1;
                
                if (op2 instanceof IntConstant) { tracedValue = op1; constVal = ((IntConstant)op2).value; }
                else if (op1 instanceof IntConstant) { tracedValue = op2; constVal = ((IntConstant)op1).value; }
                
                if (tracedValue instanceof Local && (constVal == 0 || constVal == 1)) {
                     List<Unit> defs = localDefs.getDefsOfAt((Local) tracedValue, u);
                     for (Unit def : defs) {
                         if (def instanceof soot.jimple.AssignStmt) {
                             Value defRight = ((soot.jimple.AssignStmt) def).getRightOp();
                             if (defRight instanceof FieldRef && ((FieldRef)defRight).getField().equals(field)) {
                                 checksField = true;
                                 boolean isEq = (condition instanceof soot.jimple.EqExpr);
                                 checkIsTrue = (isEq) ? (constVal == 1) : (constVal == 0);
                                 break;
                             }
                         }
                     }
                }
                
                if (!checksField) continue;
                
                Unit trueTarget = ifStmt.getTarget();
                Unit falseTarget = null;
                for (Unit succ : graph.getSuccsOf(ifStmt)) {
                    if (succ != trueTarget) { falseTarget = succ; break; }
                }
                
                boolean pdTrue = postDomFinder.isDominatedBy(trueTarget, updateUnit);
                boolean pdFalse = (falseTarget != null) && postDomFinder.isDominatedBy(falseTarget, updateUnit);
                boolean pdIf = postDomFinder.isDominatedBy(ifStmt, updateUnit);
                boolean isControlDependent = ((pdTrue && !pdFalse) || (!pdTrue && pdFalse)) && !pdIf;
                
                if (isControlDependent) {
                    boolean conditionHolds = pdTrue;
                    boolean impliedStateIsTrue = conditionHolds ? checkIsTrue : !checkIsTrue;
                    ESGNode preStateNode = boolNodes.get(impliedStateIsTrue ? "true" : "false");
                    if (preStateNode != null) {
                         esg.addEdge(preStateNode, methodNode, ESGEdge.EdgeType.STATE_TRANSITION, "guarded_by_" + (impliedStateIsTrue ? "TRUE" : "FALSE"));
                    }
                }
            }
        }
    }

    private void findGuardedPreState(ExecutionSemanticGraph esg, UnitGraph graph, Unit updateUnit, SootField field, SootClass enumClass, Map<String, ESGNode> enumNodes, ESGNode methodNode) {
        // Use Post-Dominator analysis for true Control Dependence
        MHGPostDominatorsFinder<Unit> postDomFinder = new MHGPostDominatorsFinder<>(graph);
        
        for (Unit u : graph) {
            if (u instanceof soot.jimple.IfStmt) {
                soot.jimple.IfStmt ifStmt = (soot.jimple.IfStmt) u;
                Value condition = ifStmt.getCondition();
                
                // Only handle equality / inequality checks 
                if (!(condition instanceof soot.jimple.EqExpr || condition instanceof soot.jimple.NeExpr)) continue;
                
                // Determine if the IfStmt checks our enum field 
                soot.jimple.BinopExpr expr = (soot.jimple.BinopExpr) condition;
                String enumConstName = null;
                if (isEnumConstant(expr.getOp1(), enumClass)) enumConstName = getEnumConstName(expr.getOp1());
                else if (isEnumConstant(expr.getOp2(), enumClass)) enumConstName = getEnumConstName(expr.getOp2());
                
                if (enumConstName == null) continue;
                
                // Determine successors 
                Unit trueTarget = ifStmt.getTarget();
                Unit falseTarget = null;
                for (Unit succ : graph.getSuccsOf(ifStmt)) {
                    if (succ != trueTarget) {
                        falseTarget = succ;
                        break;
                    }
                }
                
                // Post-dominance checks 
                boolean pdTrue = postDomFinder.isDominatedBy(trueTarget, updateUnit);
                boolean pdFalse = (falseTarget != null) && postDomFinder.isDominatedBy(falseTarget, updateUnit);
                boolean pdIf = postDomFinder.isDominatedBy(ifStmt, updateUnit);
                
                // Control dependence condition (Ferrante et al., 1987) 
                boolean isControlDependent = ((pdTrue && !pdFalse) || (!pdTrue && pdFalse)) && !pdIf;
                
                if (isControlDependent) {
                    ESGNode preStateNode = enumNodes.get(enumConstName);
                    if (preStateNode != null) {
                        boolean isEq = (condition instanceof soot.jimple.EqExpr);
                        String label = pdTrue 
                                ? (isEq ? "guarded_by_eq" : "guarded_by_ne") 
                                : (isEq ? "guarded_by_ne" : "guarded_by_eq");
                        
                        esg.addEdge(preStateNode, methodNode, ESGEdge.EdgeType.STATE_TRANSITION, label);
                    }
                }
            }
        }
    }
    
    private boolean isEnumConstant(Value v, SootClass enumClass) {
        if (v instanceof FieldRef) {
            return ((FieldRef)v).getField().getDeclaringClass().equals(enumClass);
        }
        return false;
    }
    
    private String getEnumConstName(Value v) {
        return ((FieldRef)v).getField().getName();
    }

    private void buildDataLayer(ExecutionSemanticGraph esg) {
         List<SootClass> appClasses = new ArrayList<>(Scene.v().getApplicationClasses());
         for (SootClass sc : appClasses) {
             // Create Data Nodes for non-boolean fields
             for (SootField field : sc.getFields()) {
                 if (!(field.getType() instanceof BooleanType)) { 
                     ESGNode dataNode = new ESGNode("DATA_" + sc.getName() + "." + field.getName(), field.getName(), ESGNode.NodeType.DATA);
                     esg.addNode(dataNode);
                     
                     for (SootMethod method : sc.getMethods()) {
                        if (!method.hasActiveBody()) continue;
                        ESGNode methodNode = getOrCreateMethodNode(esg, method);
                        Body body = method.getActiveBody();
                        UnitGraph graph = new soot.toolkits.graph.ExceptionalUnitGraph(body);
                        // Optimize LocalDefs/Uses: only compute if we find a field read
                        SimpleLocalDefs localDefs = null;
                        SimpleLocalUses localUses = null;

                        for (Unit u : body.getUnits()) {
                            if (u instanceof soot.jimple.AssignStmt) {
                                soot.jimple.AssignStmt assign = (soot.jimple.AssignStmt) u;
                                
                                // Detect new object allocation
                                if (assign.getRightOp() instanceof NewExpr) {
                                    NewExpr newExpr = (NewExpr) assign.getRightOp();
                                    String allocationSite = getAllocationSite(newExpr);
                                    ESGNode objectNode = new ESGNode("DATA_" + sc.getName() + "." + field.getName(), field.getName(), ESGNode.NodeType.DATA, allocationSite);
                                    esg.addNode(objectNode);
                                    esg.addEdge(methodNode, objectNode, ESGEdge.EdgeType.CAUSAL, "allocates");
                                }
                                
                                // 1. Check Write: field = ...
                                if (assign.getLeftOp() instanceof FieldRef) {
                                    FieldRef leftRef = (FieldRef) assign.getLeftOp();
                                    if (leftRef.getField().equals(field)) {
                                        esg.addEdge(methodNode, dataNode, ESGEdge.EdgeType.CAUSAL, "writes");
                                    }
                                }
                                
                                // 2. Check Read & Propagate: local = field
                                if (assign.getRightOp() instanceof FieldRef) {
                                    FieldRef rightRef = (FieldRef) assign.getRightOp();
                                    if (rightRef.getField().equals(field)) {
                                        // Found a read into a local variable
                                        Value local = assign.getLeftOp();
                                        if (local instanceof Local) {
                                            if (localDefs == null) {
                                                localDefs = new SimpleLocalDefs(graph);
                                                localUses = new SimpleLocalUses(graph, localDefs);
                                            }
                                            
                                            // Trace uses of this local
                                            List<UnitValueBoxPair> uses = localUses.getUsesOf(u);
                                            for (UnitValueBoxPair usePair : uses) {
                                                Unit useUnit = usePair.getUnit();
                                                Stmt useStmt = (Stmt) useUnit;
                                                
                                                // Case A: Used in method call -> method(local)
                                                if (useStmt.containsInvokeExpr()) {
                                                    SootMethod invoked = useStmt.getInvokeExpr().getMethod();
                                                    if (invoked.getDeclaringClass().isApplicationClass()) {
                                                        ESGNode targetMethodNode = getOrCreateMethodNode(esg, invoked);
                                                        esg.addEdge(dataNode, targetMethodNode, ESGEdge.EdgeType.CAUSAL, "read_and_passed_to");
                                                    }
                                                }
                                                
                                                // Case B: Used in return -> return local
                                                if (useStmt instanceof soot.jimple.ReturnStmt) {
                                                    // Mark method as "exposing" this data
                                                    esg.addEdge(dataNode, methodNode, ESGEdge.EdgeType.CAUSAL, "returned_by");
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                     }
                 }
             }
         }
    }
    
    private String getAllocationSite(NewExpr newExpr) {
        // Simple allocation site string
        return newExpr.getType().toString();
    }

    private ESGNode getOrCreateMethodNode(ExecutionSemanticGraph esg, SootMethod method) {
        return getOrCreateMethodNode(esg, method, null);
    }

    private ESGNode getOrCreateMethodNode(ExecutionSemanticGraph esg, SootMethod method, String allocationSite) {
        String id = method.getSignature() + (allocationSite != null ? "_" + allocationSite : "");
        ESGNode existing = esg.getNode(id);
        if (existing != null) return existing;
        
        ESGNode node = new ESGNode(id, method.getName(), ESGNode.NodeType.METHOD, allocationSite);
        esg.addNode(node);
        return node;
    }
}
