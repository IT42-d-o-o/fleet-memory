#!/usr/bin/env python3
"""Generate a self-contained blind grading page from grading_pairs.json.

The page never reveals which set is which arm. Votes persist to localStorage
after every keystroke so closing the tab cannot lose work.
"""
import json
import pathlib

HERE = pathlib.Path(__file__).parent
pairs = json.loads((HERE / "grading_pairs.json").read_text(encoding="utf-8"))

# Strip the answer key out of what reaches the browser. Order is preserved, so
# the vote at index i maps back to pairs[i] when scoring.
public = [{"query": p["query"], "a": p["a"], "b": p["b"]} for p in pairs]

HTML = """<!doctype html>
<meta charset="utf-8">
<title>Blind grading — fusion A/B</title>
<style>
:root { --bg:#fff; --fg:#111; --mut:#666; --line:#ddd; --card:#fafafa; --hi:#0a5; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#15171a; --fg:#e8e8e8; --mut:#9aa0a6; --line:#2c3034; --card:#1c1f23; --hi:#4ade80; }
}
* { box-sizing: border-box; }
body { margin:0; padding:20px; background:var(--bg); color:var(--fg);
       font:15px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif; }
header { display:flex; justify-content:space-between; align-items:baseline;
         border-bottom:1px solid var(--line); padding-bottom:10px; margin-bottom:16px; }
h1 { font-size:15px; margin:0; font-weight:600; }
.prog { color:var(--mut); font-variant-numeric:tabular-nums; }
.query { background:var(--card); border:1px solid var(--line); border-radius:8px;
         padding:12px 14px; margin-bottom:16px; white-space:pre-wrap; }
.query b { display:block; font-size:11px; text-transform:uppercase; letter-spacing:.06em;
           color:var(--mut); margin-bottom:6px; font-weight:600; }
.cols { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
@media (max-width:820px) { .cols { grid-template-columns:1fr; } }
.col { border:1px solid var(--line); border-radius:8px; padding:12px 14px; background:var(--card); }
.col h2 { font-size:13px; margin:0 0 10px; }
ol { margin:0; padding-left:20px; }
li { margin-bottom:8px; }
li.shared { color:var(--mut); }
.bar { margin-top:18px; display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
button { font:inherit; padding:8px 14px; border:1px solid var(--line); border-radius:6px;
         background:var(--bg); color:var(--fg); cursor:pointer; }
button:hover { border-color:var(--hi); }
.hint { color:var(--mut); font-size:13px; margin-top:12px; }
textarea { width:100%; height:180px; margin-top:12px; font:12px ui-monospace,monospace;
           background:var(--card); color:var(--fg); border:1px solid var(--line);
           border-radius:6px; padding:10px; }
label { color:var(--mut); font-size:13px; }
.done { text-align:center; padding:40px 0; }
.done h2 { color:var(--hi); }
</style>
<header>
  <h1>Which result set better answers the query?</h1>
  <div class="prog"><span id="pos">1</span> / <span id="tot">0</span>
    &nbsp;·&nbsp; graded <span id="cnt">0</span></div>
</header>
<div id="app"></div>
<div class="bar">
  <button onclick="vote('a')">← A better</button>
  <button onclick="vote('b')">B better →</button>
  <button onclick="vote('tie')">T · tie</button>
  <button onclick="vote('skip')">S · skip</button>
  <button onclick="back()">⌫ back</button>
  <label><input type="checkbox" id="dim" checked onchange="render()"> dim rows present in both</label>
</div>
<div class="hint">Keys: <b>←</b> A · <b>→</b> B · <b>T</b> tie · <b>S</b> skip · <b>Backspace</b> back.
  Judge relevance to the query only — ignore ordering and length. Progress saves automatically.</div>
<div id="out"></div>
<script>
const PAIRS = __DATA__;
const KEY = "fusion-grade-v1";
let votes = JSON.parse(localStorage.getItem(KEY) || "[]");
let i = Math.min(votes.length, PAIRS.length - 1);
document.getElementById("tot").textContent = PAIRS.length;

function esc(s){ return s.replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }

function render(){
  const app = document.getElementById("app");
  document.getElementById("cnt").textContent = votes.filter(v => v && v !== "skip").length;
  if (votes.length >= PAIRS.length){
    document.getElementById("pos").textContent = PAIRS.length;
    app.innerHTML = '<div class="done"><h2>All ' + PAIRS.length + ' graded.</h2>' +
      '<p>Copy the JSON below and paste it back to Claude.</p></div>';
    document.getElementById("out").innerHTML =
      '<textarea readonly onclick="this.select()">' + esc(JSON.stringify(votes)) + '</textarea>';
    return;
  }
  const p = PAIRS[i];
  const dim = document.getElementById("dim").checked;
  const setA = new Set(p.a), setB = new Set(p.b);
  const li = (rows, other) => rows.map(t =>
      '<li class="' + (dim && other.has(t) ? 'shared' : '') + '">' + esc(t) + '</li>').join("");
  document.getElementById("pos").textContent = i + 1;
  app.innerHTML =
    '<div class="query"><b>search query</b>' + esc(p.query) + '</div>' +
    '<div class="cols">' +
      '<div class="col"><h2>SET A</h2><ol>' + li(p.a, setB) + '</ol></div>' +
      '<div class="col"><h2>SET B</h2><ol>' + li(p.b, setA) + '</ol></div>' +
    '</div>';
  document.getElementById("out").innerHTML = "";
}

function vote(v){
  if (votes.length >= PAIRS.length) return;
  votes[i] = v;
  localStorage.setItem(KEY, JSON.stringify(votes));
  i = votes.length;
  window.scrollTo(0,0);
  render();
}
function back(){
  if (!votes.length) return;
  votes.pop();
  localStorage.setItem(KEY, JSON.stringify(votes));
  i = votes.length;
  render();
}
addEventListener("keydown", e => {
  if (e.key === "ArrowLeft") vote("a");
  else if (e.key === "ArrowRight") vote("b");
  else if (e.key.toLowerCase() === "t") vote("tie");
  else if (e.key.toLowerCase() === "s") vote("skip");
  else if (e.key === "Backspace") { e.preventDefault(); back(); }
});
render();
</script>
"""

out = HERE / "grade.html"
out.write_text(HTML.replace("__DATA__", json.dumps(public, ensure_ascii=False)),
               encoding="utf-8")
print(f"wrote {out}  ({len(public)} pairs, answer key withheld from the page)")
