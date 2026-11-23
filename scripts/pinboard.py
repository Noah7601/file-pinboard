#!/usr/bin/env python3
"""파일 핀보드 화면 (1단계: 보기 전용). 제안 CSV를 코르크 보드 위의 카드와 빨간 실로 보여 준다.

  pinboard.py --root <마스터> --proposals <제안.csv> [--checks <체크기 보고서 폴더>] [--port 8766] [--no-browser]

제안 CSV 열: src, dest, why[, approved]. src는 마스터 기준 상대 경로(마스터 밖 원본은 절대 경로), dest는 마스터 기준 폴더.
카드마다 미리 판정한다(이름·크기만 보고 본문은 읽지 않음 → 클라우드 파일을 내려받지 않음):
  옮길 수 있음 · 같은 이름 있음(같은 크기 → 중복일 수 있음 / 다른 크기 → ' (2)'로 들어감) · 새 폴더 필요 · 이미 그 자리 · 원본 없음 · 마스터 밖 목적지
이 화면은 아무것도 옮기거나 쓰지 않는다. 127.0.0.1에서만 열리고, 다른 사이트의 요청은 거절한다."""
import argparse, csv, json, os, sys, threading, time, webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fileorg_lib import nfc, resolve_nfc, is_dataless
from control_tower import read_checks

OK = {"✓", "V", "v", "예", "y", "Y"}


def judge(root, row):
    """One proposal row → card dict (read-only: lstat and listdir only)."""
    R = os.path.abspath(root)
    src_txt, dest_txt = (row.get("src") or "").strip(), (row.get("dest") or "").strip()
    outside_src = os.path.isabs(src_txt)
    src = src_txt if outside_src else resolve_nfc(R, src_txt)
    dest_dir = os.path.normpath(os.path.join(R, dest_txt))
    card = {"src": nfc(src_txt), "dest": nfc(dest_txt), "why": nfc(row.get("why") or ""),
            "approved": nfc(row.get("approved") or "").strip() in OK, "name": nfc(os.path.basename(src_txt.rstrip("/\\"))),
            "from": nfc(os.path.dirname(src_txt)) or "(맨 위)", "outside_src": outside_src,
            "size": None, "zero": False, "cloud": False, "flags": [], "state": "ok"}
    if outside_src:
        card["from"] = nfc(os.path.basename(os.path.dirname(src_txt))) + " (마스터 밖)"
    if os.path.isabs(dest_txt) or os.path.relpath(dest_dir, R).startswith(".."):
        card["state"] = "bad"; card["flags"].append("마스터 밖 목적지"); return card
    if not src or not os.path.isfile(src):
        card["state"] = "bad"; card["flags"].append("원본 없음"); return card
    st = os.lstat(src); card["size"] = st.st_size; card["zero"] = st.st_size == 0; card["cloud"] = is_dataless(src)
    if nfc(os.path.dirname(os.path.abspath(src))) == nfc(dest_dir):
        card["state"] = "skip"; card["flags"].append("이미 그 자리"); return card
    if not os.path.isdir(dest_dir):
        card["state"] = "warn"; card["flags"].append("새 폴더 필요")
    else:
        hit = next((e for e in os.listdir(dest_dir) if nfc(e) == card["name"]), None)
        if hit:
            same = os.path.getsize(os.path.join(dest_dir, hit)) == st.st_size
            card["state"] = "warn"
            card["flags"].append("같은 이름·같은 크기 있음(중복일 수 있음)" if same else "같은 이름·다른 크기 있음(' (2)'로 들어감)")
    if card["zero"]: card["flags"].append("0바이트")
    if card["cloud"]: card["flags"].append("클라우드 전용")
    return card


def build_board(root, proposals, checks_dir=None):
    with open(proposals, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    cards = [dict(judge(root, r), id=i) for i, r in enumerate(rows, 1)]
    holes = {}
    for prog in read_checks(checks_dir):
        for t in prog["todo"]:
            holes.setdefault(nfc(t.get("folder", "")), []).append({"item": t.get("item", ""), "status": t.get("status", "")})
    cols = {}
    for c in cards:
        if c["state"] == "bad" and "마스터 밖 목적지" in c["flags"]: key = "(마스터 밖 — 옮기지 않음)"
        else: key = c["dest"] or "(맨 위)"
        col = cols.setdefault(key, {"dest": key, "exists": os.path.isdir(os.path.join(root, c["dest"])), "cards": [], "holes": holes.get(nfc(os.path.basename(key.rstrip("/"))), [])})
        col["cards"].append(c["id"])
    counts = {k: sum(1 for c in cards if c["state"] == k) for k in ("ok", "warn", "skip", "bad")}
    counts["approved"] = sum(c["approved"] for c in cards); counts["total"] = len(cards)
    return {"root": nfc(os.path.abspath(root)), "proposals": nfc(os.path.abspath(proposals)), "cards": cards,
            "columns": sorted(cols.values(), key=lambda c: (-len(c["cards"]), c["dest"])), "counts": counts,
            "at": time.strftime("%Y-%m-%d %H:%M:%S")}


def make_handler(a, port):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *x): pass
        def allowed(self):
            if (self.headers.get("Host") or "").lower() not in (f"localhost:{port}", f"127.0.0.1:{port}"): return False
            return self.headers.get("Sec-Fetch-Site") in (None, "same-origin", "none")
        def send(self, code, body, ctype="application/json; charset=utf-8"):
            b = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store"); self.send_header("X-Content-Type-Options", "nosniff"); self.end_headers(); self.wfile.write(b)
        def do_GET(self):
            if not self.allowed(): return self.send(403, {"ok": False, "error": "forbidden"})
            u = urlparse(self.path)
            if u.path == "/": return self.send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            if u.path == "/api/board":
                try: return self.send(200, {"ok": True, "board": build_board(a.root, a.proposals, a.checks)})
                except (OSError, csv.Error, KeyError) as e: return self.send(500, {"ok": False, "error": str(e)})
            self.send(404, {"ok": False})
        def do_POST(self): self.send(405, {"ok": False, "error": "1단계는 보기 전용입니다"})
    return H


PAGE = r"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>파일 핀보드</title>
<style>
:root{--bar:#2b2118;--cork:#c49c6c;--frame:#6e4a2a;--paper:#fffdf5;--ink:#1f2328;--muted:#5f636e;--red:#c0292b;--green:#1a7f37;--amber:#9a6700;--blue:#0969da;--yellow:#fff096}
*{box-sizing:border-box}body{margin:0;background:var(--bar);color:var(--ink);font:14px -apple-system,"Apple SD Gothic Neo","Malgun Gothic","Noto Sans KR",sans-serif}
header{display:flex;flex-wrap:wrap;align-items:center;gap:10px 16px;padding:12px 18px;color:#ffecc8}
header h1{font-size:19px;margin:0}header .note{font-size:12.5px;color:#d8c3a5}
.chips{display:flex;flex-wrap:wrap;gap:6px}.chip{border-radius:14px;padding:3px 11px;font-size:12.5px;background:#463626;color:#f3e2c8;cursor:pointer;border:1px solid transparent}
.chip.on{border-color:#ffecc8}.chip b{color:#fff}
button{font:inherit;border:0;border-radius:8px;padding:6px 12px;background:#ffecc8;color:var(--bar);cursor:pointer}
#board{position:relative;margin:0 14px 14px;padding:18px;border:7px solid var(--frame);border-radius:6px;background:var(--cork) radial-gradient(rgba(0,0,0,.08) 1px,transparent 1.5px) 0 0/9px 9px;display:grid;grid-template-columns:minmax(260px,330px) 1fr;gap:26px;min-height:70vh}
#strings{position:absolute;inset:0;pointer-events:none;overflow:visible}
.lane h2{font-size:14px;margin:0 0 10px;color:#fff;text-shadow:0 1px 2px rgba(0,0,0,.5)}
.group{margin-bottom:14px}.group h3{font-size:12px;margin:0 0 6px;color:#3a2a18}
.card{position:relative;background:var(--paper);border:1px solid #d9cfb8;box-shadow:2px 3px 6px rgba(0,0,0,.25);padding:14px 10px 8px;margin:0 0 10px;cursor:pointer;transition:transform .15s}
.card:nth-child(odd){transform:rotate(-1deg)}.card:nth-child(even){transform:rotate(.8deg)}.card:hover,.card.sel{transform:rotate(0) scale(1.02);z-index:3}
.pin{position:absolute;left:50%;top:-7px;width:14px;height:14px;margin-left:-7px;border-radius:50%;background:var(--red);box-shadow:inset -2px -2px 0 rgba(0,0,0,.25),0 1px 2px rgba(0,0,0,.4)}
.card .nm{font-weight:700;word-break:break-all}.card .meta{font-size:11.5px;color:var(--muted);margin-top:3px}
.tags{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px}.tag{font-size:11px;padding:1px 6px;border-radius:4px;color:#fff;background:var(--muted)}
.tag.ok{background:var(--green)}.tag.warn{background:var(--amber)}.tag.bad{background:var(--red)}.tag.skip{background:#8a8f98}.tag.ap{background:var(--blue)}.tag.na{background:#b9b2a3;color:var(--ink)}
.card.dim{opacity:.35}
.cols{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:18px;align-content:start}
.col{position:relative;background:rgba(255,253,245,.92);border:1px solid #d9cfb8;box-shadow:2px 3px 6px rgba(0,0,0,.2);padding:16px 10px 10px}
.col.new{border:2px dashed var(--amber)}.col h4{margin:0 0 6px;font-size:13px;word-break:break-all}.col .sub{font-size:11.5px;color:var(--muted)}
.col ul{margin:6px 0 0;padding-left:16px;font-size:12px}.col li.on{color:var(--blue);font-weight:700}
.hole{margin-top:8px;border:2px dashed var(--red);padding:5px 7px;font-size:12px;color:var(--red);background:rgba(255,235,233,.7)}
#detail{position:fixed;right:16px;bottom:16px;width:min(420px,calc(100vw - 32px));background:var(--paper);border:1px solid #d9cfb8;border-radius:10px;box-shadow:0 8px 24px rgba(0,0,0,.4);padding:14px 16px;display:none;z-index:9}
#detail h5{margin:0 0 6px;font-size:15px;word-break:break-all}#detail dl{display:grid;grid-template-columns:max-content 1fr;gap:4px 10px;margin:0;font-size:12.5px}#detail dt{color:var(--muted)}#detail dd{margin:0;word-break:break-all}
#err{color:#ffd1d1;padding:0 18px}
@media (max-width:760px){#board{grid-template-columns:1fr}}
</style></head><body>
<header><h1>📌 파일 핀보드</h1><span class="note">1단계 · 보기 전용 — 이 화면은 아무것도 옮기지 않습니다</span>
<div class="chips" id="chips"></div><button id="reload">다시 읽기</button></header>
<div id="err"></div>
<div id="board"><svg id="strings"></svg><section class="lane"><h2>붙어 있는 파일 (옮길 후보)</h2><div id="left"></div></section><section class="lane"><h2>옮겨 꽂을 자리</h2><div class="cols" id="right"></div></section></div>
<div id="detail"></div>
<script>
const $=s=>document.querySelector(s); let B=null, filt="all", sel=null;
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmt=n=>n==null?"–":n<1024?n+" B":n<1048576?(n/1024).toFixed(1)+" KB":(n/1048576).toFixed(1)+" MB";
const STATE={ok:["옮길 수 있음","ok"],warn:["확인 필요","warn"],skip:["이미 그 자리","skip"],bad:["못 옮김","bad"]};
function show(c){if(filt==="all")return true;if(filt==="ap")return c.approved;if(filt==="na")return !c.approved;return c.state===filt}
async function load(){
  $("#err").textContent="";
  try{const r=await fetch("/api/board",{cache:"no-store"});const j=await r.json();if(!j.ok)throw new Error(j.error);B=j.board;render()}
  catch(e){$("#err").textContent="읽기 실패: "+e.message}
}
function render(){
  const k=B.counts, chips=[["all","전체",k.total],["ok","옮길 수 있음",k.ok],["warn","확인 필요",k.warn],["bad","못 옮김",k.bad],["skip","이미 그 자리",k.skip],["ap","승인됨",k.approved],["na","미승인",k.total-k.approved]];
  $("#chips").innerHTML=chips.map(([f,t,n])=>`<span class="chip${filt===f?" on":""}" data-f="${f}">${t} <b>${n}</b></span>`).join("");
  document.querySelectorAll(".chip").forEach(e=>e.onclick=()=>{filt=e.dataset.f;render()});
  const groups={}; B.cards.forEach(c=>(groups[c.from]=groups[c.from]||[]).push(c));
  $("#left").innerHTML=Object.entries(groups).map(([g,cs])=>`<div class="group"><h3>${esc(g)}</h3>${cs.map(c=>`
    <div class="card${show(c)?"":" dim"}${sel===c.id?" sel":""}" id="c${c.id}" data-id="${c.id}"><span class="pin"></span>
      <div class="nm">${esc(c.name)}</div><div class="meta">${fmt(c.size)} → ${esc(c.dest||"(맨 위)")}</div>
      <div class="tags"><span class="tag ${STATE[c.state][1]}">${STATE[c.state][0]}</span>${c.approved?'<span class="tag ap">✓ 승인됨</span>':'<span class="tag na">미승인</span>'}${c.flags.map(f=>`<span class="tag ${/원본 없음|마스터 밖|0바이트/.test(f)?"bad":"warn"}">${esc(f)}</span>`).join("")}</div></div>`).join("")}</div>`).join("");
  $("#right").innerHTML=B.columns.map((col,i)=>`<div class="col${col.exists?"":" new"}" id="k${i}" data-dest="${esc(col.dest)}"><span class="pin"></span>
    <h4>${esc(col.dest)}</h4><div class="sub">${col.exists?"있는 폴더":"새 폴더가 필요"} · 카드 ${col.cards.length}</div>
    <ul>${col.cards.map(id=>{const c=B.cards[id-1];return `<li class="${sel===id?"on":""}">${esc(c.name)}</li>`}).join("")}</ul>
    ${col.holes.map(h=>`<div class="hole">? ${esc(h.item)} — ${esc(h.status)}</div>`).join("")}</div>`).join("");
  document.querySelectorAll(".card").forEach(e=>e.onclick=()=>{sel=+e.dataset.id===sel?null:+e.dataset.id;render();detail()});
  requestAnimationFrame(strings); detail();
}
function strings(){
  const svg=$("#strings"), bd=$("#board").getBoundingClientRect(); svg.setAttribute("width",bd.width); svg.setAttribute("height",bd.height);
  const wide=getComputedStyle($("#board")).gridTemplateColumns.split(" ").length>1;
  let out=""; B.columns.forEach((col,i)=>{const t=$("#k"+i); if(!t)return; const tr=t.getBoundingClientRect();
    const tx=(wide?tr.left:tr.left+tr.width/2)-bd.left, ty=tr.top-bd.top+(wide?22:2);
    col.cards.forEach(id=>{const c=B.cards[id-1], e=$("#c"+id); if(!e||!show(c))return; const r=e.getBoundingClientRect();
      const x=(wide?r.right:r.left+r.width/2)-bd.left, y=r.top-bd.top+(wide?r.height/2:2), hot=sel===null||sel===id, dx=Math.max(40,(tx-x)/2);
      const d=wide?`M${x},${y} C${x+dx},${y} ${tx-dx},${ty} ${tx},${ty}`:`M${x},${y} L${tx},${ty}`;
      out+=`<path d="${d}" fill="none" stroke="#c0292b" stroke-width="${sel===id?3.4:2}" opacity="${hot?(sel===null?.6:1):.1}"${c.state==="bad"?' stroke-dasharray="5 5"':""}/>`+
           (wide?`<circle cx="${x}" cy="${y}" r="3.5" fill="#c0292b" opacity="${hot?.9:.15}"/><circle cx="${tx}" cy="${ty}" r="4.5" fill="#c0292b" opacity="${hot?.9:.15}"/>`:"")})});
  svg.innerHTML=out;
}
function detail(){
  const d=$("#detail"); if(sel===null){d.style.display="none";return}
  const c=B.cards[sel-1]; d.style.display="block";
  d.innerHTML=`<h5>${esc(c.name)}</h5><dl><dt>지금 위치</dt><dd>${esc(c.src)}</dd><dt>옮길 곳</dt><dd>${esc(c.dest||"(맨 위)")}</dd><dt>근거</dt><dd>${esc(c.why||"–")}</dd>
  <dt>크기</dt><dd>${fmt(c.size)}${c.cloud?" · 클라우드 전용":""}</dd><dt>판정</dt><dd>${STATE[c.state][0]}${c.flags.length?" — "+esc(c.flags.join(", ")):""}</dd><dt>승인</dt><dd>${c.approved?"✓ 승인됨":"아직 미승인"}</dd></dl>
  <p style="font-size:12px;color:#5f636e;margin:10px 0 0">기준 폴더: ${esc(B.root)}<br>제안 파일: ${esc(B.proposals)} · 읽은 시각 ${esc(B.at)}</p>`;
}
$("#reload").onclick=load; addEventListener("resize",()=>B&&strings()); load();
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", required=True); ap.add_argument("--proposals", required=True)
    ap.add_argument("--checks", help="체크기 보고서 폴더(*_check.md, *_warnings.csv) — 빈자리 표시")
    ap.add_argument("--port", type=int, default=8766); ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    build_board(a.root, a.proposals, a.checks)          # fail fast on a bad CSV or path
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(a, a.port))
    url = f"http://127.0.0.1:{a.port}/"
    print(f"파일 핀보드(보기 전용): {url}  · 끝내려면 Ctrl+C", flush=True)
    if not a.no_browser: threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try: srv.serve_forever()
    except KeyboardInterrupt: pass


if __name__ == "__main__":
    main()
