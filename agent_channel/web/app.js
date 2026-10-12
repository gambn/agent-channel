let currentMode = "node";
let channelRunning = false;

// 面板里的节点 tag、错误消息等都可能来自外部（订阅内容、后端回显），
// 一律转义后再拼进 innerHTML，避免恶意节点名注入脚本。
function escapeHtml(value) {
	return String(value == null ? "" : value)
		.replace(/&/g, "&amp;")
		.replace(/</g, "&lt;")
		.replace(/>/g, "&gt;")
		.replace(/"/g, "&quot;")
		.replace(/'/g, "&#39;");
}

// 写接口统一走 JSON：服务端强制 `Content-Type: application/json`，无 header 的
// 裸 POST 会被 415 挡掉。
function postJson(path, payload) {
	return fetch(path, {
		method: "POST",
		headers: { "Content-Type": "application/json" },
		body: JSON.stringify(payload || {}),
	});
}

// 退出整套程序：服务端先回响应，再停通道、还原系统代理、关面板。
// 确认一步是有意的——这一下会把正在跑的通道也停掉。
async function handleQuit() {
	if (!confirm("退出后通道会停止、系统代理会还原。确定退出吗？")) {
		return;
	}
	const btn = document.getElementById("btn-quit");
	btn.disabled = true;
	btn.textContent = "正在退出…";
	try {
		const res = await postJson("/api/quit", {});
		const data = await res.json();
		document.body.innerHTML =
			'<div style="padding:40px;font-family:system-ui;font-size:14px">' +
			(data.message || "已退出") +
			"<br><br>本页可以关掉了。</div>";
	} catch (err) {
		btn.disabled = false;
		btn.textContent = "退出程序";
		alert("退出请求失败：" + err);
	}
}

function switchMode(mode) {
	currentMode = mode;
	["node", "sub", "json"].forEach((m) => {
		document.getElementById("tab-" + m).classList.toggle("active", m === mode);
		document.getElementById("panel-" + m).style.display =
			m === mode ? "flex" : "none";
	});
	// json 标签无保存按钮：导入即写入，不需要“保存配置”
	document.getElementById("save-bar").style.display =
		mode === "json" ? "none" : "flex";
	loadOutboundOptions();
}

async function loadConfig() {
	try {
		const res = await fetch("/api/config");
		const data = await res.json();
		if (data.active_source) switchMode(data.active_source);
		if (data.node_uri)
			document.getElementById("input-node").value = data.node_uri;
		if (data.subscription_url)
			document.getElementById("input-sub").value = data.subscription_url;
		if (data.imported_count != null)
			document.getElementById("imported-count").innerText = data.imported_count;
		await loadOutboundOptions();
	} catch (e) {
		console.error("加载配置失败", e);
	}
}

async function loadOutboundOptions() {
	try {
		const res = await fetch("/api/outbound_options?source=" + currentMode);
		const data = await res.json();
		const sel = document.getElementById("select-outbound");
		sel.length = 0;

		const n = data.available_nodes || 0;
		if (n === 0) {
			const opt = document.createElement("option");
			opt.value = "";
			opt.text = "未配置节点";
			sel.appendChild(opt);
			sel.disabled = true;
			return;
		}
		if (n === 1) {
			// 单节点来源：只读显示「将使用」
			const node = data.options[0];
			const opt = document.createElement("option");
			opt.value = node.key;
			opt.text = "将使用：" + node.tag + " [" + node.type + "]";
			sel.appendChild(opt);
			sel.disabled = true;
			return;
		}

		// 池来源（≥2）：auto + 各节点
		sel.disabled = false;
		const selectedKey = data.selected
			? data.selected.mode === "auto"
				? "auto"
				: data.selected.node_key
			: null;
		(data.options || []).forEach((o) => {
			const opt = document.createElement("option");
			opt.value = o.key;
			opt.text =
				o.key === "auto"
					? o.latency_ms != null
						? o.tag +
							"（本批最快：" +
							(o.fastest_tag || "") +
							" " +
							o.latency_ms +
							" ms）"
						: o.tag
					: o.tag +
						" [" +
						o.type +
						"] " +
						(o.latency_ms != null ? o.latency_ms + " ms" : "—");
			if (selectedKey && o.key === selectedKey) opt.selected = true;
			sel.appendChild(opt);
		});
	} catch (e) {
		console.error("加载出站选项失败", e);
	}
}

async function handleOutboundChange() {
	const key = document.getElementById("select-outbound").value;
	const wasRunning = channelRunning;
	const payload =
		key === "auto"
			? { mode: "auto", source: currentMode }
			: { mode: "pinned", node_key: key, source: currentMode };
	try {
		const res = await fetch("/api/set_outbound", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify(payload),
		});
		const data = await res.json();
		if (!data.success) {
			alert(data.error || "设置出站失败");
			return;
		}
		// 选中即写配置并刷新状态，不建通道；运行中切出站只提示，不自动重启
		refreshStatus();
		if (wasRunning) showOutboundHint("已切换，需重启通道生效");
	} catch (e) {
		alert("设置出站失败: " + e);
	}
}

function showOutboundHint(text) {
	const hint = document.getElementById("outbound-hint");
	hint.innerText = text;
	hint.style.display = "inline";
	setTimeout(() => {
		hint.style.display = "none";
	}, 4000);
}

async function handleSaveConfig() {
	// 保存即切换生效来源：发当前标签的 active_source（与 CLI set-node/set-sub 同口径，
	// 配完即用），让下次守护启动按这个来源接通；槽字段只发当前标签，json 无保存按钮。
	const payload = { active_source: currentMode };
	if (currentMode === "node") {
		payload.node_uri = document.getElementById("input-node").value.trim();
	} else if (currentMode === "sub") {
		payload.subscription_url = document
			.getElementById("input-sub")
			.value.trim();
	}
	try {
		const res = await fetch("/api/config", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify(payload),
		});
		const result = await res.json();
		if (result.success) {
			const hint = document.getElementById("save-hint");
			hint.style.display = "inline";
			setTimeout(() => {
				hint.style.display = "none";
			}, 3000);
			loadOutboundOptions();
			refreshStatus();
		} else {
			alert("保存失败");
		}
	} catch (e) {
		alert("请求异常: " + e);
	}
}

async function refreshStatus() {
	try {
		const res = await fetch("/api/status");
		const data = await res.json();
		const isRunning = data.status === "running";
		channelRunning = isRunning;

		document.getElementById("global-badge").className =
			"badge " + (isRunning ? "badge-running" : "badge-stopped");
		document.getElementById("global-badge").innerText = isRunning
			? "运行中"
			: "已停止";
		document.getElementById("status-mode").innerText =
			data.mode === "standalone" ? "独立内核转发" : "-";
		document.getElementById("status-port").innerText = data.proxy_url || "-";
		document.getElementById("status-source").innerText =
			data.source_label || "-";
		document.getElementById("status-node").innerText =
			data.node_tag || (isRunning ? "本地直通" : "-");

		// 系统代理开关状态同步
		const sysProxy = data.system_proxy;
		const isSysOn = sysProxy && sysProxy.enabled;
		const toggleEl = document.getElementById("toggle-sysproxy");
		const tagEl = document.getElementById("sysproxy-tag");
		toggleEl.checked = isSysOn;
		tagEl.innerText = isSysOn ? "已同步系统代理" : "未同步";
		tagEl.style.color = isSysOn ? "var(--green-tag-text)" : "var(--text-muted)";
		tagEl.style.background = isSysOn
			? "var(--green-soft-bg)"
			: "var(--neutral-btn)";
		tagEl.style.border = isSysOn
			? "1px solid var(--green-light-border)"
			: "1px solid var(--card-border)";
	} catch (e) {
		console.error(e);
	}
}

async function handleToggleSysProxy(checkbox) {
	const willEnable = checkbox.checked;
	try {
		const res = await fetch("/api/sysproxy", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ enabled: willEnable }),
		});
		const data = await res.json();
		if (!data.success) {
			alert(data.error || "系统代理切换失败");
			checkbox.checked = !willEnable;
		}
	} catch (e) {
		alert("请求异常: " + e);
		checkbox.checked = !willEnable;
	}
	refreshStatus();
}

async function handleStart() {
	const btn = document.getElementById("btn-start");
	const payload = {
		source: currentMode,
		set_sys_proxy: document.getElementById("toggle-sysproxy").checked,
	};
	if (currentMode === "node") {
		payload.node_uri = document.getElementById("input-node").value.trim();
	} else if (currentMode === "sub") {
		payload.subscription_url = document
			.getElementById("input-sub")
			.value.trim();
	}
	// json：候选来自 imported_nodes（导入即写入），无地址字段可传

	// 立即反馈：启用要拉订阅/排序/起内核/转发验证，可能几秒到几十秒；
	// 不置灰 + 改文案的话，按钮看起来像死的（用户反馈的“反应滞后”）。
	const prevText = btn ? btn.innerText : "";
	if (btn) {
		btn.disabled = true;
		btn.innerText = "启动中…";
	}
	try {
		const res = await fetch("/api/enable", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify(payload),
		});
		const result = await res.json();
		if (result.error) {
			alert("启动失败: " + result.error);
		} else if (result.source && result.source !== currentMode) {
			// 回执与请求不一致：不静默，报警
			alert(
				"生效来源与所选标签不一致：请求 " +
					currentMode +
					"，实际 " +
					result.source,
			);
		}
	} catch (e) {
		alert("请求异常: " + e);
	} finally {
		if (btn) {
			btn.disabled = false;
			btn.innerText = prevText;
		}
	}
	refreshStatus();
	refreshGeolocation();
}

async function handleStop() {
	const btn = document.getElementById("btn-stop");
	const prevText = btn ? btn.innerText : "";
	if (btn) {
		btn.disabled = true;
		btn.innerText = "停止中…";
	}
	try {
		const res = await postJson("/api/disable");
		const data = await res.json();
		if (data.error) {
			// 409：启停共用一把锁，上一次"启动/切换"还没走完。如实说清，别静默
			alert("停止失败: " + data.error);
		}
	} catch (e) {
		alert("停止请求异常: " + e);
	} finally {
		if (btn) {
			btn.disabled = false;
			btn.innerText = prevText;
		}
	}
	refreshStatus();
}

async function handleGenUUID() {
	try {
		const res = await fetch("/api/uuid");
		const data = await res.json();
		document.getElementById("input-uuid").value = data.uuid;
		navigator.clipboard.writeText(data.uuid);
	} catch (e) {
		alert("生成 UUID 失败: " + e);
	}
}

async function loadCoreInfo() {
	try {
		const res = await fetch("/api/core_info");
		const data = await res.json();
		const badge = document.getElementById("core-badge");
		const statusEl = document.getElementById("core-status");
		badge.innerText = data.local_version;
		if (data.local_version !== "未安装") {
			badge.style.background = "var(--green-soft-bg)";
			badge.style.color = "var(--green-tag-text)";
			badge.style.border = "1px solid var(--green-light-border)";
			statusEl.innerText =
				"本地内核版本: " + data.local_version + " (点击右侧检查是否有新版)";
		} else {
			statusEl.innerText = "本地尚未安装内核，点击右侧检查并下载";
		}
	} catch (e) {}
}

async function handleCheckCore() {
	const statusEl = document.getElementById("core-status");
	const updateBtn = document.getElementById("btn-update-core");
	const badge = document.getElementById("core-badge");
	statusEl.innerText = "正在请求官方 Release 元数据...";
	updateBtn.style.display = "none";

	try {
		const res = await fetch("/api/check_update");
		const data = await res.json();
		if (data.local_version) {
			badge.innerText = data.local_version;
		}

		if (data.is_latest) {
			statusEl.innerText = "✅ 已是最新版本 (" + data.latest_version + ")";
			statusEl.style.color = "var(--green-primary)";
			updateBtn.style.display = "none";
		} else if (data.has_update) {
			statusEl.innerText =
				"检测到官方新版本: " +
				data.latest_version +
				" (当前本地: " +
				data.local_version +
				")";
			statusEl.style.color = "var(--green-deep)";
			updateBtn.innerText = "立即更新至 " + data.latest_version;
			updateBtn.style.display = "inline-block";
		} else {
			statusEl.innerText = data.status_message || "检查完毕";
			statusEl.style.color = "var(--text-muted)";
		}
	} catch (e) {
		statusEl.innerText = "请求失败: " + e;
		statusEl.style.color = "#dc2626";
	}
}

async function handleUpdateCore() {
	const updateBtn = document.getElementById("btn-update-core");
	const statusEl = document.getElementById("core-status");
	const badge = document.getElementById("core-badge");
	updateBtn.disabled = true;
	updateBtn.innerText = "正在下载与更新...";

	try {
		const res = await postJson("/api/update_core");
		const data = await res.json();
		if (data.success) {
			statusEl.innerText =
				"✅ 内核已成功更新至 " + data.version + "！已是最新版本";
			statusEl.style.color = "var(--green-primary)";
			badge.innerText = data.version;
			badge.style.background = "var(--green-soft-bg)";
			badge.style.color = "var(--green-tag-text)";
			badge.style.border = "1px solid var(--green-light-border)";
			updateBtn.style.display = "none";
			alert("内核更新成功！");
		} else {
			statusEl.innerText = "❌ 更新失败: " + data.error;
			statusEl.style.color = "#dc2626";
			updateBtn.disabled = false;
			updateBtn.innerText = "重试更新";
		}
	} catch (e) {
		statusEl.innerText = "更新异常: " + e;
		updateBtn.disabled = false;
		updateBtn.innerText = "重试更新";
	}
}

async function handleTestAI() {
	const targets = ["openai", "gemini", "anthropic"];
	targets.forEach((t) => {
		document.getElementById("test-" + t).innerText = "测试中...";
		document.getElementById("test-" + t).style.color = "var(--text-muted)";
	});

	try {
		const res = await postJson("/api/test_ai");
		const data = await res.json();
		// 诚实标注：通道没跑时这是直连结果，不能让“测试通过”看起来像“通道没问题”
		const hint = document.getElementById("test-via-hint");
		if (hint) {
			hint.innerText = data.note
				? "⚠ " + data.note
				: "本次探测经通道出口（via=" + (data.via || "channel") + "）";
			hint.style.display = "block";
		}
		targets.forEach((t) => {
			const item = data[t];
			const el = document.getElementById("test-" + t);
			if (item && item.reachable) {
				el.innerText = "● 连通正常 (" + item.latency_ms + "ms)";
				el.style.color = "var(--green-primary)";
			} else {
				el.innerText = "✕ 失败 (" + (item ? item.error : "连接超时") + ")";
				el.style.color = "#dc2626";
			}
		});
	} catch (e) {
		alert("AI 测试请求异常: " + e);
	}
}

function renderBenchmarkResults(data, resultEl) {
	if (data.error) {
		resultEl.innerHTML =
			'<span style="color:#dc2626;">✕ ' + escapeHtml(data.error) + "</span>";
		return;
	}
	const srcLabel =
		{ node: "单节点", sub: "订阅", json: "JSON" }[data.source] || "";
	let html = srcLabel
		? '<div style="color:var(--text-muted);margin-bottom:6px;">来源：' +
			escapeHtml(srcLabel) +
			"</div>"
		: "";
	data.results.forEach((r) => {
		const tcp = r.tcp_reachable ? escapeHtml(r.tcp_ms) + "ms" : "不可达";
		let fwd;
		if (r.forwarding === true)
			fwd = '<span style="color:var(--green-primary);">转发正常</span>';
		else if (r.forwarding === false)
			fwd = '<span style="color:#dc2626;">转发失败</span>';
		else fwd = '<span style="color:var(--text-muted);">未验证</span>';
		let landing = "";
		if (r.landing_ip) {
			landing =
				'<span style="color:var(--text-muted);">落地 ' +
				escapeHtml(r.landing_ip) +
				"</span>";
			if (r.is_relay === true) {
				landing += ' <span style="color:#dc2626;font-weight:600;">中转</span>';
			}
		}
		html +=
			'<div style="padding:6px 0;border-bottom:1px solid var(--card-border);display:flex;align-items:center;gap:8px;">' +
			'<span style="font-weight:600;">' +
			escapeHtml(r.tag || r.server) +
			"</span> " +
			'<span style="color:var(--text-muted);">[' +
			escapeHtml(r.type) +
			" " +
			escapeHtml(r.server) +
			":" +
			escapeHtml(r.server_port) +
			"]</span> " +
			"<span>TCP " +
			tcp +
			"</span> " +
			fwd +
			landing +
			"</div>";
	});
	resultEl.innerHTML = html;
}

async function handleBenchmark() {
	const resultEl = document.getElementById("benchmark-result");
	resultEl.innerHTML = "测速中，请稍候...";

	try {
		const res = await fetch("/api/benchmark", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			// 按面板当前标签测（与出站下拉同一口径）：切到订阅就测订阅，不是 active_source。
			body: JSON.stringify({ source: currentMode }),
		});
		const data = await res.json();
		renderBenchmarkResults(data, resultEl);
		loadOutboundOptions();
	} catch (e) {
		resultEl.innerHTML =
			'<span style="color:#dc2626;">✕ 测速失败: ' + escapeHtml(e) + "</span>";
	}
}

function handleClearBenchmark() {
	document.getElementById("benchmark-result").innerHTML = "";
}

async function handleImportNodes() {
	const fileInput = document.getElementById("input-import-file");
	const resultEl = document.getElementById("import-result");
	if (!fileInput.files || !fileInput.files.length) {
		resultEl.innerHTML =
			'<span style="color:#dc2626;">请先选择 JSON 文件</span>';
		return;
	}
	const text = await fileInput.files[0].text();
	try {
		const res = await fetch("/api/import_nodes", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ text: text }),
		});
		const data = await res.json();
		if (data.error) {
			resultEl.innerHTML =
				'<span style="color:#dc2626;">✕ ' + escapeHtml(data.error) + "</span>";
			return;
		}
		const skipped =
			(data.errors ? data.errors.length : 0) + (data.duplicates || 0);
		let html =
			'<span style="color:var(--green-primary);">✅ 导入 ' +
			escapeHtml(data.imported) +
			" 个，跳过 " +
			escapeHtml(skipped) +
			" 个</span>";
		(data.errors || []).forEach((e) => {
			html +=
				'<div style="color:var(--text-muted);">· ' + escapeHtml(e) + "</div>";
		});
		if (data.duplicates) {
			html +=
				'<div style="color:var(--text-muted);">· 重复 ' +
				escapeHtml(data.duplicates) +
				" 个（已存在，跳过）</div>";
		}
		(data.renamed || []).forEach((e) => {
			html +=
				'<div style="color:var(--text-muted);">· ' + escapeHtml(e) + "</div>";
		});
		resultEl.innerHTML = html;
		fileInput.value = "";
		document.getElementById("imported-count").innerText = data.total;
		loadOutboundOptions();
		refreshStatus();
	} catch (e) {
		resultEl.innerHTML =
			'<span style="color:#dc2626;">✕ 导入失败: ' + escapeHtml(e) + "</span>";
	}
}

async function handleClearImportedNodes() {
	const resultEl = document.getElementById("import-result");
	try {
		const res = await fetch("/api/import_nodes", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ action: "clear" }),
		});
		const data = await res.json();
		if (data.error) {
			resultEl.innerHTML =
				'<span style="color:#dc2626;">✕ ' + escapeHtml(data.error) + "</span>";
			return;
		}
		resultEl.innerHTML =
			'<span style="color:var(--green-primary);">✅ 已清空导入节点</span>';
		document.getElementById("imported-count").innerText = data.total;
		loadOutboundOptions();
		refreshStatus();
	} catch (e) {
		resultEl.innerHTML =
			'<span style="color:#dc2626;">✕ 清空失败: ' + escapeHtml(e) + "</span>";
	}
}

async function refreshGeolocation() {
	const el = document.getElementById("status-geo");
	try {
		const res = await fetch("/api/geolocation");
		const data = await res.json();
		if (data.error) {
			el.innerText = "-";
			return;
		}
		const parts = [data.city, data.region, data.country]
			.filter(Boolean)
			.join(" · ");
		const isp = data.isp ? "（" + data.isp + "）" : "";
		let text = parts + isp;
		if (data.weather && data.weather.temp_c != null) {
			text +=
				"｜ " + data.weather.temp_c + "°C " + weatherDesc(data.weather.code);
		}
		el.innerText = text;
	} catch (e) {
		el.innerText = "-";
	}
}

function weatherDesc(code) {
	const map = {
		0: "晴",
		1: "多云",
		2: "多云",
		3: "阴",
		45: "雾",
		48: "雾凇",
		51: "毛毛雨",
		53: "毛毛雨",
		55: "毛毛雨",
		61: "小雨",
		63: "中雨",
		65: "大雨",
		71: "小雪",
		73: "中雪",
		75: "大雪",
		80: "阵雨",
		81: "阵雨",
		82: "强阵雨",
		95: "雷雨",
		96: "雷雨冰雹",
		99: "强雷雨冰雹",
	};
	return map[code] || "";
}

// 页面初始化
loadConfig();
loadCoreInfo();
handleGenUUID();
refreshStatus();
refreshGeolocation();
setInterval(refreshStatus, 3000);
