# ESG Construction for Spark

This module constructs the Execution Semantic Graph (ESG) for the Spark Master project.

## Structure

The logic is implemented in Java using the Soot framework and is located in:
`src/main/java/com/esg/`

## How to Run

1.  Navigate to this directory:
    ```bash
    cd /root/MyIntention/intent_driven_testing/src/EFG_construction
    ```

2.  Compile the project:
    ```bash
    mvn compile
    ```

3.  Run the analyzer:
    ```bash
    mvn exec:java -Dexec.mainClass="com.esg.Main"
    ```

## Notes

-   The analyzer targets the `spark-master` source code located at `../../data/raw/spark-master/src/main/java`.
-   Soot is configured to run in "phantom refs" mode to handle missing dependencies, as Spark is a large project with many external libraries.
-   The output will be saved to `spark_esg.dot` in the current directory.
