"""Source-backed test inputs; unknown Make/shell expressions stay explicit."""
import posixpath
import re


def expand(value, variables):
    for _ in range(len(variables) + 1):
        updated = re.sub(r'\$\((\w+)\)|\$\{(\w+)\}|\$(\w+)',
                         lambda m: variables.get(next(g for g in m.groups() if g), m[0]), value)
        if updated == value:
            break
        value = updated
    return None if '$' in value or '`' in value else value


def closure(unit, paths, read):
    paths = set(paths)
    edges, unknown, submodules = [], [], set(unit.get('submodules', []))
    inputs = set(unit['sources'])
    directory = unit['directory']
    gitlinks = set(re.findall(r'^\s*path\s*=\s*(.*?)\s*$', read('.gitmodules'), re.M))
    variables = {'CONTIKI': '.'}
    for line in read('Makefile.dir-variables').splitlines():
        match = re.match(r'\s*(\w+)\s*[:?]?=\s*([^#]*)', line)
        if match:
            variables[match[1]] = match[2].strip()
    variables['CONTIKI'] = '.'

    def add(source, target, kind):
        target = posixpath.normpath(target)
        if target in paths:
            edge = {'from': source, 'to': target, 'kind': kind}
            if edge not in edges:
                edges.append(edge)
            inputs.add(target)
            return target

    application_dirs = {directory}
    if unit.get('kind') == 'cooja':
        application_dirs.update(posixpath.dirname(p) for p in inputs if p.endswith('.c'))
    pending = []
    for application in sorted(application_dirs):
        makefile = add(unit['id'], application + '/Makefile', 'application-makefile')
        add(unit['id'], application + '/project-conf.h', 'automatic-project-config')
        if makefile:
            pending.append((makefile, 0, application))
    seen = set()
    while pending:
        path, depth, application = pending.pop(0)
        if (path, application) in seen:
            continue
        seen.add((path, application))
        text = re.sub(r'\\\n', ' ', read(path))
        for line in text.splitlines():
            match = re.match(r'\s*(MODULES|PROJECT_SOURCEFILES)\s*[:+?]?=\s*([^#]*)', line)
            if not match:
                continue
            for token in match[2].split():
                value = expand(token, variables)
                if value is None:
                    unknown.append({'source': path, 'expression': token, 'kind': match[1]})
                    continue
                if match[1] == 'PROJECT_SOURCEFILES':
                    target = value if token.startswith(('$(CONTIKI)/', '${CONTIKI}/')) else posixpath.join(application, value)
                    add(path, target, 'project-source')
                else:
                    value = posixpath.normpath(value)
                    if value in gitlinks and value in paths:
                        submodules.add(value)
                    module_make = value + '/Makefile.' + posixpath.basename(value)
                    found = add(path, module_make, 'module-makefile')
                    if found and depth < 2:
                        pending.append((found, depth + 1, application))
                    macros = value + '/module-macros.h'
                    add(path, macros, 'module-macros')

    # Resolve local C includes recursively. Production headers stay dependencies,
    # not automatically protected test support or additions to the test patch.
    pending = sorted(p for p in inputs if p.endswith(('.c', '.h')))
    seen = set()
    while pending:
        path = pending.pop(0)
        if path in seen:
            continue
        seen.add(path)
        for name in re.findall(r'^\s*#\s*include\s*"([^"\n]+)"', read(path), re.M):
            target = posixpath.normpath(posixpath.join(posixpath.dirname(path), name))
            if target in paths:
                add(path, target, 'local-c-include')
                if target.endswith(('.c', '.h')):
                    pending.append(target)

    # Upstream shell runners execute from the suite directory, including when
    # they source another file. Preserve that cwd rather than using its parent.
    cwd = unit['suite_directory']
    pending = [(p, {}) for p in sorted(inputs) if p.endswith('.sh')]
    seen = set()
    while pending:
        path, values = pending.pop(0)
        key = (path, tuple(sorted(values.items())))
        if key in seen:
            continue
        seen.add(key)
        for line in read(path).splitlines():
            assignment = re.match(r'\s*(?:export\s+)?(\w+)=(\S+)\s*$', line)
            if assignment:
                value = expand(assignment[2].strip('\"\''), values)
                if value is not None:
                    values[assignment[1]] = value
            source = re.match(r'\s*(?:source|\.)\s+([^;\s]+)', line)
            if not source:
                source = re.match(r'\s*(?:exec\s+)?((?:\./|\.\./)[^;\s]+\.sh)(?:\s|$)', line)
            if source:
                value = expand(source[1].strip('\"\''), values)
                target = add(path, posixpath.join(cwd, value), 'sourced-shell') if value is not None else None
                if target:
                    pending.append((target, dict(values)))
                elif value is None:
                    unknown.append({'source': path, 'expression': source[1], 'kind': 'shell-source'})
            for pattern in re.findall(r'\b(?:in|cat|grep)\s+(\$[^\s;]+/\*)', line):
                value = expand(pattern.rstrip('*').strip('\"\''), values)
                if value is not None:
                    prefix = posixpath.normpath(posixpath.join(cwd, value)) + '/'
                    for target in sorted(p for p in paths if p.startswith(prefix)):
                        add(path, target, 'shell-input-glob')
                else:
                    unknown.append({'source': path, 'expression': pattern, 'kind': 'shell-input-glob'})

    return {'sources': sorted(inputs), 'submodules': sorted(submodules),
            'dependency_evidence': {'method': 'static-source-closure',
                                    'edges': sorted(edges, key=lambda e: (e['from'], e['to'], e['kind'])),
                                    'unresolved': sorted(unknown, key=lambda e: (e['source'], e['expression'], e['kind'])),
                                    'build_verified': False}}
