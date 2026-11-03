# Direct input dependencies are the authoritative source lineage

Status: accepted design direction; the overall technical design remains in draft.

Each node result, proposal revision, export, and memory artifact retains relations to its direct inputs. A reader's access check follows those relations to every original source and applies current authorization to the full dependency closure. Displayed citations are separate evidence references; removing one does not remove a source that actually contributed to the artifact. An unresolved or cyclic chain denies access.

Storing only a flattened list of original sources would make reads simpler but would obscure how intermediate results inherited restrictions. A flattened closure may be rebuilt as a read projection, while direct relations remain the authority. A proposal revision and its direct relations are saved together; a derived artifact spanning stores stays unavailable to business readers until its authoritative lineage is complete and verified. Detailed storage constraints, cross-store recovery, and bounded traversal remain to be specified before implementation.
