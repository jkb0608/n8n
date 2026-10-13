# JavaScript for C/C++ readers

Written for someone who programs C/C++ and has never written JavaScript. Each
section answers one question: **what is different here, and what will bite me?**
The last two sections cover the API that is *not* JavaScript at all - the n8n
runtime - and the patterns this workflow uses over and over.

---

## 1. Five things that bite C programmers first

### 1.1 Truthiness - `0`, `''`, `null`, `undefined`, `NaN` are all FALSE

C has no boolean context: `if (x)` means `x != 0`. JavaScript coerces *any* value
to a boolean:

| value | `if (v)` | note |
|---|---|---|
| `0`, `-0`, `NaN` | **false** | a perfectly valid number that is falsy |
| `''` (empty string) | false | |
| `null`, `undefined` | false | two different "no value" |
| `[]`, `{}` | **true** | an *empty* container is still truthy |
| `'0'`, `'false'` | **true** | any non-empty string is truthy |

So `if (count)` is **wrong** when `count` may legitimately be `0`. Write
`if (count > 0)` or `if (count !== 0)`.

### 1.2 `null` vs `undefined` - two different "no value"

* `undefined` - declared but never assigned; a missing property; a function that
  returns nothing.
* `null` - explicitly "empty", assigned by you.

C has only `NULL`. In JS, `x == null` (loose) is true for **both**, while
`x === null` is true only for `null`. Touching a property of either throws:
`undefined.x` → `TypeError`. That is why this workflow writes
`const j = $input.first().json || {};` before reading `j.something`.

### 1.3 `||` is neither `?:` nor bitwise-or

`a || b` evaluates to `a` when `a` is truthy, otherwise `b` - it **returns the
value**, not a bool:

```js
const n = input.count || 3;   // count === 0  ->  n becomes 3    (usually a BUG)
const n = input.count ?? 3;   // only null / undefined -> 3     (usually correct)
```

* `??` = nullish coalescing: fall back **only** for `null` / `undefined`.
* `||` and `&&` short-circuit and return an **operand**, which is why
  `j.error && j.error.message` works as a guarded property access.
* `&&` does not produce a bool either: `a && b` is `a` when `a` is falsy.

### 1.4 `const` is not a type

`const` only means "this binding cannot be reassigned" - it is **not** `const int`.
The value stays mutable:

```js
const o = {};   o.x = 1;    // fine
const a = [1];  a.push(2);  // fine
const n = 1;    n = 2;      // TypeError
```

Use `let` when you intend to reassign; `var` is the legacy form (function-scoped,
avoid it).

### 1.5 Numbers are all doubles; `===` is the strict comparison

There is no `int` / `long`: every number is an IEEE-754 double (safe integers up to
2^53). `===` / `!==` compare **value and type**; `==` / `!=` coerce first
(`'1' == 1` is true) - always use `===`.

---

## 2. Values, objects, arrays

| C/C++ | JavaScript | note |
|---|---|---|
| `struct { int a; }` | `{ a: 1 }` | **dynamic**: keys may be added/removed at any time, no declaration |
| `std::vector<T>` / array | `[1, 2, 3]` | grows freely, mixed types allowed |
| `x.a` / `x->a` | `x.a` | and `x['a']` for a computed key |
| `std::map` | `{ k: v }` or `Map` | object keys are strings |
| `nullptr` | `null` or `undefined` | see 1.2 |
| `a ? b : c` | `a ? b : c` | identical |
| `sizeof(a)/sizeof(a[0])` | `a.length` | property, not a function |

**Arrays** carry higher-order methods that replace hand-written loops - think
`std::for_each` / `std::transform`, but chained:

```js
const nums  = [1, 2, 3, 4];
nums.map(n => n * 2);            // [2,4,6,8]      transform (returns new array)
nums.filter(n => n > 2);         // [3,4]          select
nums.find(n => n > 2);           // 3              first match, else undefined
nums.reduce((acc, n) => acc + n, 0);  // 10       accumulate
nums.some(n => n > 3);           // true           "any"
nums.every(n => n > 0);          // true           "all"
nums.includes(2);                // true           membership
```

`(n) => n * 2` is an **arrow function** ≈ a lambda: `[](int n){ return n * 2; }`.
It captures by reference automatically (no `[&]` needed).

Other methods used in this repo: `.slice(a, b)` (substring / sub-array - note it is
`slice`, not "substr"), `.join(sep)` (concatenate with a separator), `.trim()`
(strip whitespace), `.sort()`, `Array.from(set)` (a `Set` is like `std::set` but
insertion-ordered and only tracks keys).

**Strings**: `'...'` and `"..."` are equivalent; `` `...` `` is a **template
literal**:

```js
const name = 'world';
const s = `hello ${name}, ${1 + 2}`;   // ≈ sprintf("hello %s, %d", ...)
```

`JSON.stringify(obj)` ≈ serialize to JSON text;
`JSON.parse(text)` ≈ deserialize (throws on bad input).

---

## 3. Control flow, functions, errors

* `if / else`, `for`, `while`, `switch`, `?:`, `break`, `continue` - same as C/C++.
* `for (const x of list)` ≈ range-for. `for (const k in obj)` iterates **keys**.
* `for (let i = 0; i < n; i++)` works as in C.
* `function f(a) {}` and `const f = (a) => {}` are two spellings of the same thing;
  the second (arrow) is what this repo uses. `return` behaves like C's, and a
  function without `return` yields `undefined`.
* `try { ... } catch (e) { ... }` ≈ C++ exceptions. `e` is the thrown value
  (usually an `Error` with `.message`). There is no `finally` in this repo, but it
  exists. Nothing forces you to declare what can throw: any call may throw.
* `throw new Error('msg')` ≈ `throw std::runtime_error("msg")`.

---

## 4. The n8n runtime API (not JavaScript!)

A Code node body is JavaScript, but it runs inside n8n with extra globals injected.
Learn these once and every Code node becomes readable:

| API | meaning | C++ analogue |
|---|---|---|
| `$input.first()` | first incoming item; `.json` is its payload | first element of the input vector |
| `$input.all()` | all incoming items as an array | the whole input vector |
| `$json` | shorthand for `$input.first().json` | current element's struct |
| `$('Node name')` | **another node's** output, by name: `.first().json`, `.all()` | read a global produced by an earlier stage |
| `$execution` | execution metadata (`id`, `mode`, …) | process context |
| `$getWorkflowStaticData('global')` | persistent key/value store across runs | a small on-disk state file |
| `$node` / `$parameter` | the current node's own config | `this` |

**The item model.** Every node passes an array of *items*; an item is
`{ json: {...}, binary?: {...} }`. A Code node must return the same shape:

```js
return [{ json: { number: 24, result: 'SUCCESS' } }];   // one item out
```

Think of it as `std::vector<Item>` where `struct Item { json::object json; }`, and
the engine connects one node's output vector to the next node's input vector.

**Execution mode** (node setting, not code): `Run Once for All Items` gives you
`$input.all()` and expects one array back; `Run Once for Each Item` runs the body
per item and returns a single item. Every Code node in this workflow is
**Run Once for All Items**.

**Errors**: a node setting `onError: continueRegularOutput` means "if this node
throws, pass an item `{ error: {...} }` to the next node instead of failing the
run". That is why several nodes read `j.error` first - a downstream node must be
able to tell "the fetch failed" from "the fetch returned this data".

---

## 5. Patterns this workflow uses everywhere

```js
// 1. defensive read: never let a missing node explode the whole run
const pick = (n) => { try { return $(n).first().json || {}; } catch (e) { return {}; } };

// 2. defensive unwrap: null/undefined -> empty object before touching properties
const j = $input.first().json || {};

// 3. "did the upstream fail?" - the response body is checked before the status code
const errMsg = String((j.error && (j.error.message || JSON.stringify(j.error))) || '');

// 4. only null/undefined fall back (NOT 0, NOT '')
const number = j.number ?? null;

// 5. bounded copy: never push a huge string further down the pipeline
const last_draft = String(j.dev_draft || '').slice(0, 1500);

// 6. "now or never" anonymous call, used to read a node that may not exist yet
const carry = (() => { try { return $('[RETRY] 计数+1').first().json.carry ?? null; } catch (e) { return null; } })();

// 7. build a multi-line text
const body = [title, 'state: ' + state, 'token: ' + JSON.stringify(t)].join('\n');

// 8. classify by regex, tolerate junk around the payload
const m = text.match(/"fixed"\s*:\s*(true|false)/);
const fixed = m ? m[1] === 'true' : false;
```

**Regex quick map**: `/abc/i` = literal, `i` = ignoreCase, `g` = global (all
matches), `\s` whitespace, `\d` digit, `[...]` character class, `(a|b)` alternation
and capture group, `m[1]` the first group, `re.test(s)` "does it match",
`s.match(re)` the groups (or `null`), `s.replace(re, x)` substitute.

---

## 6. How to read a Code node in this workflow

1. Read the **header comment** - it states *what / input / output*.
2. Find the `return [{ json: {...} }]` at the end: that is the node's **contract**,
   the only thing downstream nodes can see.
3. Anything reading `$('...')` reaches *backwards* into an earlier node; anything
   reading `$input` / `$json` only sees the previous node.
4. Comments marked **【C/C++ 读者注意】**flag exactly where JS differs from what you
   would expect in C/C++.

## 7. Prove the comments changed nothing

Comments and layout are invisible to a parser, so the repository ships a checker
that parses the Code nodes with **acorn** (the parser n8n itself bundles) and
compares the abstract syntax trees before/after an edit:

```bash
# 1. freeze the baseline (before editing)
n8n export:workflow --id=OS20kZOaniS17HKw --output=dump/baseline/wf_baseline.json
node tools/jsast_equiv.mjs baseline --dump dump/baseline/wf_baseline.json --out dump/jsast_baseline.json

# 2. after editing
n8n export:workflow --id=OS20kZOaniS17HKw --output=dump/wf.json
node tools/jsast_equiv.mjs verify --baseline dump/jsast_baseline.json --dump dump/wf.json

# 3. is the checker itself any good? (comment+reindent must pass, real edits must fail)
node tools/jsast_equiv.mjs selftest --dump dump/baseline/wf_baseline.json
```

`verify` prints one line per Code node (`ast_full` / `ast_sem` / `literals` /
`prefix`) and exits non-zero on any difference. It also recomputes `[PROMPT]`'s
frozen-prefix `prefix_hash`, so a single accidental byte inside the cached prompt
prefix fails the check even though it would still parse. A regex that strips `//`
comments must **not** be used for this: these files contain
`https://api.deepseek.com` and `http://127.0.0.1:8080/...`, and a line-oriented
regex would silently cut those lines.
