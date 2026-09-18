# Graph conventions

An edge points from a source node to the immediate derived node. The root is the requested target column. All nodes must be reachable by following edges upstream from that root. All terminal nodes must appear in `mappings`; repeat a terminal source when distinct branches or dependency kinds need separate descriptions.

A node identifies a column at a particular definition/state. `id` is unique within this trace. `scope` records the job, file, statement or notebook cell and write version where needed. Never merge two `tmp_result.amount` nodes solely because their displayed table and column names match. Preserve the scope in the JSON graph even though the requested ten CSV fields cannot represent it unambiguously.

Use empty schema for schema-less CTEs/temporary dataframes; keep their actual object names. Put catalog qualification in schema. For file sources, use the literal path in table and the actual field in column. For literal/runtime nodes, schema and table may be empty and column is the literal or function/parameter expression. For unknown identifiers preserve the observed text and use source_type `unresolved`.

`expanded` nodes have upstream edges. Other terminal nodes have no incoming edges and explain the stopping reason. `repo_boundary` requires a physical/view/file source, cited use, and actual producer-search notes. A known `current_timestamp()` is runtime; an unknown job parameter changing a source identifier is unresolved. A cycle terminal represents a separately scoped previous state and explains why further provenance cannot be resolved. An unresolved/cycle node prevents a complete result.

# Derivations

For edges, expression means the immediate expression plus relevant row-selection context. For mappings, expression means the full derivation from that terminal source to the requested root. Use a labeled, ordered sequence of source expressions when composing a single SQL expression would change semantics; do not invent executable SQL across incompatible dialects.

Example: if target `net` reads `tmp.net`, and `tmp.net` is `gross - COALESCE(discount, 0)`, create both source dependencies to tmp.net and the hop to target.net. The root mappings have separate gross and discount rows; both carry the full composed derivation with the zero-default behavior. A filter on `status = 'active'` gets a filter dependency and plain English stating that only active records contribute.

Source Type describes the source node, not confidence. Dependency kinds are value, join, filter, group, window, order or control. Business rules describe code behavior; do not infer unstated business policy. The exporter prefixes dependency kinds in the business-rule field because the requested CSV has no dedicated kind field.

For COUNT(*), row existence is a dependency: use a row-set pseudo-column `*` with an explanation and trace the contributing relation and its filters. DISTINCT, aggregation, lateral/explode, null extension in outer joins, QUALIFY and window frames can change values or row membership even if no projected expression names their columns.

# Conditional source forms

For templated SQL or configuration-driven source names, cite both the expression and the binding. Preserve alternative branches when context does not select one; mark unresolved if their applicability cannot be established.

For Python/Spark, follow dataframe variables, withColumn/select expressions, joins, UDF definitions, spark.sql string construction, temp view registration and writes. A dataframe variable and a registered SQL view may be the same data at a particular execution point; demonstrate the link.

For notebooks, follow referenced notebooks and cell execution dependencies evidenced by the job. Cite raw repository file lines and optionally explain cell identity in scope. Do not assume interactive execution history is represented by file order.

For MERGE and updates, model each action separately, including match predicates, defaults, retained prior values and incremental watermarks. If the initial population or ordering is unavailable, represent the missing previous state. Never label a recursive or historical dependency complete merely because following it repeats a table name.
