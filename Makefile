.PHONY: gen-types gen-types-ts gen-types-py test-bridge

# Generate Python pydantic model from the shared JSON Schema.
gen-types-py:
	datamodel-codegen \
		--input schemas/inbound_envelope.schema.json \
		--input-file-type jsonschema \
		--output connecting_dots/generated/inbound_envelope.py \
		--output-model-type pydantic_v2.BaseModel \
		--class-name InboundEnvelope \
		--use-schema-description \
		--use-double-quotes \
		--target-python-version 3.11

# Generate TS types from the shared JSON Schema.
gen-types-ts:
	npm run gen:types

# Generate both.
gen-types: gen-types-ts gen-types-py

# Round-trip test: TS serializes -> Python parses via codegenned types.
test-bridge:
	node scripts/emit_envelope.mjs > /tmp/_envelope.json
	python -m scripts.parse_envelope /tmp/_envelope.json
