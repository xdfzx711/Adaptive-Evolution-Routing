"""Domain models; import public types from their named schema modules.

RunContext and EvolutionRequest carry live runtime handles. Persist their
to_record() values; all other domain DTOs use model_dump_json/model_validate_json.
"""
