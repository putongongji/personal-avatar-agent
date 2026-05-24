const refreshButton = document.querySelector("#refresh");
const runEvalSampleButton = document.querySelector("#runEvalSample");
const runEvalAllButton = document.querySelector("#runEvalAll");
const evalStatus = document.querySelector("#evalStatus");
const evalModal = document.querySelector("#evalModal");
const evalModalTitle = document.querySelector("#evalModalTitle");
const evalModalSubtitle = document.querySelector("#evalModalSubtitle");
const evalModalBody = document.querySelector("#evalModalBody");
const evalModalClose = document.querySelector("#evalModalClose");

let evalCasesCache = [];

function setText(id, value) {
  document.querySelector(`#${id}`).textContent = value;
}

function fillRows(id, rows, emptyMessage) {
  const body = document.querySelector(`#${id}`);
  body.innerHTML = "";
  if (!rows.length) {
    const row = document.createElement("tr");
    const columnCount = body.closest("table")?.querySelectorAll("thead th").length || 6;
    row.innerHTML = `<td colspan="${columnCount}" class="muted">${emptyMessage}</td>`;
    body.appendChild(row);
    return;
  }
  rows.forEach((cells) => {
    const row = document.createElement("tr");
    row.innerHTML = cells.map((cell) => `<td>${cell}</td>`).join("");
    body.appendChild(row);
  });
}

function fillHtml(id, html, emptyMessage) {
  const body = document.querySelector(`#${id}`);
  body.innerHTML = html || `<tr><td colspan="5" class="muted">${escapeHtml(emptyMessage)}</td></tr>`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function renderInline(text) {
  return escapeHtml(text)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`(.+?)`/g, "<code>$1</code>");
}

function renderSimpleMarkdown(markdown) {
  const lines = String(markdown || "").split(/\r?\n/);
  const html = [];
  let inList = false;
  function closeList() {
    if (inList) {
      html.push("</ul>");
      inList = false;
    }
  }
  for (const rawLine of lines) {
    const line = rawLine.trimEnd();
    if (!line.trim()) {
      closeList();
      continue;
    }
    const heading = line.match(/^(#{2,4})\s+(.+)$/);
    if (heading) {
      closeList();
      html.push(`<h3>${renderInline(heading[2])}</h3>`);
      continue;
    }
    const bullet = line.match(/^[-*]\s+(.+)$/);
    if (bullet) {
      if (!inList) {
        html.push("<ul>");
        inList = true;
      }
      html.push(`<li>${renderInline(bullet[1])}</li>`);
      continue;
    }
    closeList();
    html.push(`<p>${renderInline(line)}</p>`);
  }
  closeList();
  return html.join("");
}

async function loadAdmin() {
  const [summaryResponse, questionsResponse, gapsResponse, evalsResponse] = await Promise.all([
    fetch("/api/admin/summary"),
    fetch("/api/admin/questions"),
    fetch("/api/admin/gaps"),
    fetch("/api/admin/evals"),
  ]);
  const summary = await summaryResponse.json();
  const questions = await questionsResponse.json();
  const gaps = await gapsResponse.json();
  const evals = await evalsResponse.json();
  evalCasesCache = evals.cases || [];

  setText("total", summary.total_questions);
  setText("answered", `${summary.answered_rate}%`);
  setText("quality", summary.avg_quality);
  setText("missing", summary.missing_count);
  setText("evalTotal", evals.total || 0);
  setText("evalRan", evals.ran || 0);
  setText("evalPassRate", `${evals.pass_rate || 0}%`);
  setText("evalScore", evals.avg_score || 0);

  fillHtml("evalCases", renderEvalCases(evalCasesCache), "还没有评测用例");
  fillRows(
    "topQuestions",
    summary.top_questions.map(([question, count]) => [escapeHtml(question), count]),
    "还没有提问记录"
  );
  fillRows(
    "categories",
    summary.category_counts.map(([category, count]) => [`<span class="tag">${escapeHtml(category)}</span>`, count]),
    "还没有分类数据"
  );
  fillRows(
    "gaps",
    gaps.gaps.map((gap) => [
      escapeHtml(gap.question),
      `<span class="tag warn">${escapeHtml(gap.category)}</span>`,
      escapeHtml(gap.missing_info || "未能回答"),
      gap.count,
    ]),
    "暂时没有资料缺口"
  );
  fillRows(
    "questions",
    questions.questions.map((question) => [
      `<div class="question-row"><strong>${escapeHtml(question.question)}</strong><span class="muted">${escapeHtml(question.created_at)}</span></div>`,
      `<span class="tag">${escapeHtml(question.category)}</span>`,
      escapeHtml(question.classification_provider || "unknown"),
      `${escapeHtml(question.retrieval_method || "none")} / ${escapeHtml((question.retrieval_stats || {}).returned_count || 0)}`,
      escapeHtml(question.answer_provider || "unknown"),
      Math.round(question.quality_score),
      `${Math.round(question.latency_ms || 0)}ms`,
      escapeHtml(question.status || "new"),
    ]),
    "还没有提问记录"
  );
}

function renderEvalStatus(status) {
  const label = status || "new";
  const className = label === "passed" ? "tag ok" : label === "failed" ? "tag warn" : "tag";
  return `<span class="${className}">${escapeHtml(label)}</span>`;
}

function dimensionLabel(key) {
  return {
    intent_classification: "分类",
    retrieval_relevance: "检索",
    answer_coverage: "覆盖",
    boundary_control: "边界",
    citation_quality: "引用",
  }[key] || key;
}

function renderDimensionPills(dimensions = {}) {
  const keys = ["intent_classification", "retrieval_relevance", "answer_coverage", "boundary_control", "citation_quality"];
  return `<div class="eval-dimensions">${keys
    .map((key) => {
      const item = dimensions[key] || {};
      const score = Math.round(item.score ?? 0);
      const state = score >= 90 ? "ok" : score >= 60 ? "warn" : "bad";
      return `<span class="eval-pill ${state}">${dimensionLabel(key)} ${score}</span>`;
    })
    .join("")}</div>`;
}

function renderList(items = [], empty = "-") {
  if (!items.length) return `<span class="muted">${escapeHtml(empty)}</span>`;
  return `<ul>${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`;
}

function renderSourceList(sources = []) {
  if (!sources.length) return `<span class="muted">无来源</span>`;
  return `<ol>${sources
    .map(
      (source) =>
        `<li><strong>${escapeHtml(source.heading || "-")}</strong><span>${escapeHtml(source.quote || "")}</span></li>`
    )
    .join("")}</ol>`;
}

function renderEvalReport(item) {
  const details = item.details || {};
  const dimensions = details.dimensions || {};
  const coverage = dimensions.answer_coverage || {};
  const retrieval = dimensions.retrieval_relevance || {};
  const boundary = dimensions.boundary_control || {};
  return `
    <div class="eval-report">
      <section>
        <h3>AI 分析</h3>
        <div id="evalAiAnalysis" class="ai-analysis">${details.ai_analysis ? renderSimpleMarkdown(details.ai_analysis) : '<p class="muted">正在生成 AI 分析...</p>'}</div>
        <button class="secondary" data-regenerate-analysis="${escapeHtml(item.id)}" type="button">重新生成 AI 分析</button>
      </section>
      <section>
        <h3>修复判断</h3>
        <p>${escapeHtml(details.diagnosis || "未运行或没有诊断信息")}</p>
        <p class="muted">主失败类型：${escapeHtml(details.primary_failure || "-")}</p>
      </section>
      <section>
        <h3>分类</h3>
        <p>期望：${escapeHtml(dimensions.intent_classification?.expected || "-")}；实际：${escapeHtml(dimensions.intent_classification?.actual || item.category || "-")}</p>
      </section>
      <section>
        <h3>检索</h3>
        <p>候选 ${escapeHtml(details.retrieval_stats?.candidate_count ?? "-")}，入选 ${escapeHtml(details.retrieval_stats?.returned_count ?? "-")}，方法 ${escapeHtml(details.retrieval_stats?.method || "-")}</p>
        <div class="eval-columns">
          <div><strong>命中来源</strong>${renderList(retrieval.matched_sources || [])}</div>
          <div><strong>缺失来源</strong>${renderList(retrieval.missing_sources || [])}</div>
        </div>
      </section>
      <section>
        <h3>答案覆盖</h3>
        <div class="eval-columns">
          <div><strong>已覆盖</strong>${renderList(coverage.covered_terms || [])}</div>
          <div><strong>已召回但没写</strong>${renderList(coverage.retrieved_not_used || [])}</div>
          <div><strong>未召回</strong>${renderList(coverage.not_retrieved || [])}</div>
        </div>
      </section>
      <section>
        <h3>边界</h3>
        <div><strong>禁止说法命中</strong>${renderList(boundary.forbidden_terms || [])}</div>
      </section>
      <section>
        <h3>实际回答</h3>
        <p>${escapeHtml(details.actual_answer_excerpt || "-")}</p>
      </section>
      <section>
        <h3>实际来源</h3>
        ${renderSourceList(details.actual_sources || [])}
      </section>
    </div>
  `;
}

function renderEvalCases(cases) {
  return cases
    .map((item) => {
      const details = item.details || {};
      const dimensions = details.dimensions || {};
      return `
        <tr>
          <td><strong>${escapeHtml(item.case_id || item.id)}</strong><span class="muted">${escapeHtml(item.last_run_at || "未运行")}</span></td>
          <td><div class="question-row"><strong>${escapeHtml(item.question)}</strong><span class="muted">${escapeHtml(item.expected?.answer_policy || "")}</span></div></td>
          <td><strong>${escapeHtml(details.primary_failure || "-")}</strong><p class="muted">${escapeHtml(details.diagnosis || (details.failures || []).join("；") || "-")}</p></td>
          <td>${renderDimensionPills(dimensions)}</td>
          <td>${renderEvalStatus(item.status)}<strong class="score">${Math.round(item.score || 0)}</strong><button class="link-button" data-open-eval="${escapeHtml(item.id)}" type="button">查看报告</button></td>
        </tr>
      `;
    })
    .join("");
}

function openEvalModal(itemId) {
  const item = evalCasesCache.find((caseItem) => String(caseItem.id) === String(itemId));
  if (!item) return;
  evalModalTitle.textContent = `${item.case_id || item.id} 评测报告`;
  evalModalSubtitle.textContent = item.question || "";
  evalModalBody.innerHTML = renderEvalReport(item);
  evalModal.classList.remove("hidden");
  evalModal.setAttribute("aria-hidden", "false");
  document.body.classList.add("modal-open");
  if (!item.details?.ai_analysis) {
    loadEvalAiAnalysis(item.id, false);
  }
}

function closeEvalModal() {
  evalModal.classList.add("hidden");
  evalModal.setAttribute("aria-hidden", "true");
  document.body.classList.remove("modal-open");
}

async function loadEvalAiAnalysis(itemId, force) {
  const target = document.querySelector("#evalAiAnalysis");
  if (target) {
    target.innerHTML = `<p class="muted">${force ? "正在重新生成 AI 分析..." : "正在生成 AI 分析..."}</p>`;
  }
  try {
    const response = await fetch(`/api/admin/evals/${itemId}/analysis`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ force }),
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || "AI 分析生成失败");
    }
    const item = evalCasesCache.find((caseItem) => String(caseItem.id) === String(itemId));
    if (item) {
      item.details = item.details || {};
      item.details.ai_analysis = data.ai_analysis;
    }
    if (target) {
      target.innerHTML = renderSimpleMarkdown(data.ai_analysis || "");
    }
  } catch (error) {
    if (target) {
      target.innerHTML = `<p class="muted">AI 分析失败：${escapeHtml(error.message)}</p>`;
    }
  }
}

async function runEval(limit) {
  runEvalSampleButton.disabled = true;
  runEvalAllButton.disabled = true;
  evalStatus.textContent = limit ? `正在运行前 ${limit} 条评测...` : "正在运行全部评测...";
  try {
    const response = await fetch("/api/admin/evals/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ limit: limit || 0 }),
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || "评测运行失败");
    }
    evalStatus.textContent = `本次运行 ${data.ran} 条，通过 ${data.passed} 条，通过率 ${data.pass_rate}%`;
    await loadAdmin();
  } catch (error) {
    evalStatus.textContent = `评测失败：${error.message}`;
  } finally {
    runEvalSampleButton.disabled = false;
    runEvalAllButton.disabled = false;
  }
}

refreshButton.addEventListener("click", loadAdmin);
runEvalSampleButton.addEventListener("click", () => runEval(5));
runEvalAllButton.addEventListener("click", () => runEval(0));
evalModalClose.addEventListener("click", closeEvalModal);
evalModal.addEventListener("click", (event) => {
  if (event.target?.matches("[data-modal-close]")) {
    closeEvalModal();
  }
  const openButton = event.target?.closest("[data-regenerate-analysis]");
  if (openButton) {
    loadEvalAiAnalysis(openButton.dataset.regenerateAnalysis, true);
  }
});
document.querySelector("#evalCases").addEventListener("click", (event) => {
  const button = event.target.closest("[data-open-eval]");
  if (button) {
    openEvalModal(button.dataset.openEval);
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !evalModal.classList.contains("hidden")) {
    closeEvalModal();
  }
});
loadAdmin();
