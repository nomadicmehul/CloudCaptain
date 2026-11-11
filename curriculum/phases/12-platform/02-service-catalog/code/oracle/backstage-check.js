'use strict';

const fs = require('fs');
const path = require('path');
const YAML = require('yaml');
const model = require('@backstage/catalog-model');
const alpha = require('@backstage/catalog-model/alpha');

const KNOWN_KINDS = ['API', 'Component', 'Domain', 'Group', 'Location', 'Resource', 'System', 'User'];
const KIND_VALIDATORS = [
  model.apiEntityV1alpha1Validator,
  model.componentEntityV1alpha1Validator,
  model.resourceEntityV1alpha1Validator,
  model.groupEntityV1alpha1Validator,
  model.locationEntityV1alpha1Validator,
  model.userEntityV1alpha1Validator,
  model.systemEntityV1alpha1Validator,
  model.domainEntityV1alpha1Validator,
];
const POLICY = model.EntityPolicies.allOf([
  new model.SchemaValidEntityPolicy(),
  new model.DefaultNamespaceEntityPolicy(),
  new model.NoForeignRootFieldsEntityPolicy(),
  new model.FieldFormatEntityPolicy(),
]);
const CATALOG_MODEL = alpha.compileCatalogModel([alpha.defaultCatalogEntityModel]);

function pointerToField(pointer) {
  return pointer
    .split('/')
    .filter(Boolean)
    .map(part => part.replace(/~1/g, '/').replace(/~0/g, '~'))
    .join('.');
}

function fieldOf(message, entity) {
  let match = /^"([^"]+)" (?:is not valid|must have a value|could not be validated)/.exec(message);
  if (match) {
    return /^(labels|annotations|tags|links)\./.test(match[1]) ? `metadata.${match[1]}` : match[1];
  }
  match = /^Unknown field (.+)$/.exec(message);
  if (match) {
    return match[1];
  }
  if (message.startsWith('Malformed envelope')) {
    try {
      model.entitySchemaValidator()(entity);
    } catch (error) {
      return fieldOf(error.message, entity);
    }
    return '<envelope>';
  }
  match = /^(<root>|\/\S*) /.exec(message);
  if (match) {
    const base = match[1] === '<root>' ? '' : pointerToField(match[1]);
    const extra = /(?:missingProperty|additionalProperty): ([^,]+)/.exec(message);
    return [base, extra ? extra[1].trim() : ''].filter(Boolean).join('.') || '<root>';
  }
  return '<unknown>';
}

function valueAt(entity, dotted) {
  return dotted.split('.').reduce((value, key) => (value && typeof value === 'object' ? value[key] : undefined), entity);
}

function relationsOf(entity, dropped) {
  const kind = CATALOG_MODEL.getKind(entity);
  if (!kind) {
    return [];
  }
  const pairs = CATALOG_MODEL.getRelations({ kind: entity.kind }) || [];
  const self = model.getCompoundEntityRef(entity);
  const selfRef = model.stringifyEntityRef(self);
  const selfNamespace = entity.metadata.namespace || model.DEFAULT_NAMESPACE;
  const out = [];
  for (const field of kind.relationFields) {
    const value = valueAt(entity, field.path);
    if (!value) {
      continue;
    }
    for (const text of (Array.isArray(value) ? value : [value]).filter(x => x && typeof x === 'string')) {
      let target;
      try {
        target = model.parseEntityRef(text, {
          defaultKind: field.defaultKind,
          defaultNamespace: field.defaultNamespace === 'default' ? model.DEFAULT_NAMESPACE : selfNamespace,
        });
      } catch (error) {
        error.field = field.path;
        throw error;
      }
      const targetKind = target.kind.toLowerCase();
      if (field.allowedKinds && !field.allowedKinds.some(k => k.toLowerCase() === targetKind)) {
        dropped.push({ ref: selfRef, field: field.path, target: text });
        continue;
      }
      const targetRef = model.stringifyEntityRef(target);
      out.push([selfRef, field.relation, targetRef]);
      const sourceKind = entity.kind.toLowerCase();
      const pair = pairs.find(
        r =>
          r.forward.type === field.relation &&
          r.fromKind.some(k => k.toLowerCase() === sourceKind) &&
          r.toKind.some(k => k.toLowerCase() === targetKind),
      );
      if (pair) {
        out.push([targetRef, pair.reverse.type, selfRef]);
      }
    }
  }
  return out;
}

async function judge(data, dropped) {
  if (!data || typeof data !== 'object' || Array.isArray(data)) {
    return { accepted: false, field: '<root>', error: `Expected object at root, got ${Array.isArray(data) ? 'array' : typeof data}` };
  }
  const entity = JSON.parse(JSON.stringify(data));
  try {
    await POLICY.enforce(entity);
  } catch (error) {
    return { accepted: false, field: fieldOf(error.message, data), error: error.message };
  }
  let recognised = false;
  for (const validator of KIND_VALIDATORS) {
    try {
      if (await validator.check(entity)) {
        recognised = true;
        break;
      }
    } catch (error) {
      return { accepted: false, field: fieldOf(error.message, entity), error: error.message };
    }
  }
  if (!recognised) {
    const field = KNOWN_KINDS.includes(entity.kind) ? 'apiVersion' : 'kind';
    return { accepted: false, field, error: `no kind validator accepted kind ${entity.kind} at ${entity.apiVersion}` };
  }
  try {
    return { accepted: true, relations: relationsOf(entity, dropped) };
  } catch (error) {
    return { accepted: false, field: error.field || '<unknown>', error: error.message };
  }
}

async function main(argv) {
  const [base, ...files] = argv;
  if (!base || files.length === 0) {
    process.stderr.write('usage: node backstage-check.js BASE FILE...\n');
    return 2;
  }
  const documents = [];
  const relations = [];
  const dropped = [];
  for (const source of files) {
    const text = fs.readFileSync(path.join(base, source), 'utf8');
    let index = 0;
    for (const document of YAML.parseAllDocuments(text)) {
      if (document.errors.length) {
        documents.push({ source, doc: index, accepted: false, field: '<yaml>', error: document.errors[0].message });
        index += 1;
        continue;
      }
      const data = document.toJSON();
      if (data === null || data === undefined) {
        continue;
      }
      const verdict = await judge(data, dropped);
      if (verdict.relations) {
        relations.push(...verdict.relations);
        delete verdict.relations;
      }
      documents.push({ source, doc: index, data, ...verdict });
      index += 1;
    }
  }
  const versions = {
    '@backstage/catalog-model': require('@backstage/catalog-model/package.json').version,
    yaml: require('yaml/package.json').version,
    node: process.version,
  };
  process.stdout.write(`${JSON.stringify({ versions, documents, relations, dropped }, null, 2)}\n`);
  return 0;
}

main(process.argv.slice(2)).then(
  code => process.exit(code),
  error => {
    process.stderr.write(`${error.stack}\n`);
    process.exit(1);
  },
);
