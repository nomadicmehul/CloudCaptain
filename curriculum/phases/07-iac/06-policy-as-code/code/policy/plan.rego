package main

writes contains rc if {
	some rc in input.resource_changes
	rc.mode == "managed"
	some action in rc.change.actions
	action in {"create", "update"}
}

creates contains rc if {
	some rc in writes
	"create" in rc.change.actions
}

deletes contains rc if {
	some rc in input.resource_changes
	rc.mode == "managed"
	"delete" in rc.change.actions
}

replacements contains rc if {
	some rc in deletes
	"create" in rc.change.actions
}

type_is(rc, patterns) if {
	some pattern in patterns
	glob.match(pattern, null, rc.type)
}

address_is(rc, patterns) if {
	some pattern in patterns
	glob.match(pattern, null, rc.address)
}

unknown(rc, path) if {
	some n in numbers.range(1, count(path))
	object.get(rc.change.after_unknown, array.slice(path, 0, n), false) == true
}

present(rc, path) if {
	object.get(rc.change.after, path, null) != null
}

violates(rc, check) if {
	unknown(rc, check.path)
	object.get(check, "unknown", "deny") != "allow"
}

violates(rc, check) if {
	check.op == "exists"
	not unknown(rc, check.path)
	not present(rc, check.path)
}

violates(rc, check) if {
	check.op == "absent"
	present(rc, check.path)
}

violates(rc, check) if {
	not check.op in {"exists", "absent"}
	present(rc, check.path)
	not holds(check, object.get(rc.change.after, check.path, null))
}

holds(check, value) if {
	check.op == "equals"
	value == check.value
}

holds(check, value) if {
	check.op == "not_equals"
	value != check.value
}

holds(check, value) if {
	check.op == "in"
	value in check.value
}

holds(check, value) if {
	check.op == "not_in"
	not value in check.value
}

holds(check, value) if {
	check.op == "matches"
	matches(check.value, value)
}

holds(check, value) if {
	check.op == "not_matches"
	not matches(check.value, value)
}

holds(check, value) if {
	check.op == "lt"
	is_number(value)
	value < check.value
}

holds(check, value) if {
	check.op == "lte"
	is_number(value)
	value <= check.value
}

holds(check, value) if {
	check.op == "gt"
	is_number(value)
	value > check.value
}

holds(check, value) if {
	check.op == "gte"
	is_number(value)
	value >= check.value
}

matches(pattern, value) if {
	is_string(value)
	regex.match(pattern, value)
}

replacing(actions) if {
	"delete" in actions
	"create" in actions
}

action_of(actions) := "replace" if {
	replacing(actions)
}

action_of(actions) := concat(",", actions) if {
	not replacing(actions)
}

finding(rule, rc, message) := result if {
	result := {
		"msg": sprintf("%s: %s (%s) %s", [rule, rc.address, action_of(rc.change.actions), message]),
		"rule": rule,
		"address": rc.address,
	}
}

deny contains result if {
	some rc in writes
	type_is(rc, ["terraform_data"])
	violates(rc, {"path": ["input", "tags", "owner"], "op": "exists"})
	result := finding("require-owner-tag", rc, "every resource carries an owner tag so an incident has someone to page")
}

deny contains result if {
	some rc in writes
	type_is(rc, ["terraform_data"])
	violates(rc, {"path": ["input", "tags", "env"], "op": "in", "value": ["dev", "staging", "prod"]})
	result := finding("require-env-tag", rc, "env is one of dev, staging, prod")
}

warn contains result if {
	some rc in creates
	type_is(rc, ["terraform_data"])
	violates(rc, {"path": ["input", "tags", "cost_center"], "op": "exists"})
	result := finding("require-cost-center", rc, "new resources carry a cost_center tag (a warning this quarter, a deny from the next)")
}

deny contains result if {
	some rc in writes
	type_is(rc, ["terraform_data"])
	violates(rc, {"path": ["input", "size"], "op": "not_in", "value": ["xlarge", "metal"]})
	result := finding("forbid-huge-sizes", rc, "xlarge and metal sizes need a capacity review")
}

deny contains result if {
	some rc in writes
	type_is(rc, ["terraform_data"])
	violates(rc, {"path": ["input", "disk_gb"], "op": "lte", "value": 500})
	result := finding("disk-limit", rc, "disks above 500 GB need a capacity review")
}

warn contains result if {
	some rc in writes
	type_is(rc, ["terraform_data"])
	violates(rc, {"path": ["input", "name"], "op": "matches", "value": `^[a-z][a-z0-9-]*$`})
	result := finding("name-convention", rc, "names are lowercase words joined by dashes")
}

deny contains result if {
	some rc in writes
	type_is(rc, ["local_file", "local_sensitive_file"])
	violates(rc, {"path": ["file_permission"], "op": "not_matches", "value": `[2367]$`})
	result := finding("no-world-writable-files", rc, "files are not world-writable; set file_permission (the provider default is 0777)")
}

deny contains result if {
	some rc in deletes
	address_is(rc, ["terraform_data.audit_log", "module.*.terraform_data.audit_log"])
	result := finding("protect-audit-log", rc, "the audit log is protected; remove it from this rule in a reviewed PR before deleting it")
}

deny contains result if {
	some rc in replacements
	address_is(rc, ["*.db", "*.db_*", "*aws_db_instance.*", "*aws_rds_cluster.*", "*google_sql_database_instance.*", "*aws_ebs_volume.*", "*aws_s3_bucket.*"])
	result := finding("no-replace-stateful", rc, "stateful resources are not replaced by a routine apply; write a migration plan")
}

module_calls contains call if {
	walk(object.get(input, ["configuration", "root_module"], {}), [path, value])
	count(path) > 1
	path[count(path) - 2] == "module_calls"
	names := [path[i] | some i, _ in path; i > 0; path[i - 1] == "module_calls"]
	call := {
		"address": concat(".", [sprintf("module.%s", [name]) | some name in names]),
		"source": object.get(value, "source", ""),
	}
}

source_allowed(source) if {
	some pattern in [`^\./modules/`, `^registry\.opentofu\.org/acme/`, `^git::https://github\.com/acme/`]
	regex.match(pattern, source)
}

deny contains result if {
	some call in module_calls
	not source_allowed(call.source)
	result := {
		"msg": sprintf("module-source-allow-list: %s (config) %s; source %s matches none of the allowed patterns", [call.address, "modules come from ./modules, the acme registry namespace or the acme Git organisation", call.source]),
		"rule": "module-source-allow-list",
		"address": call.address,
	}
}
