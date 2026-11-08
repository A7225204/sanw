window.__ModuleLoader__.load({
	id: "dsh-plugin-dafeiyu-dashboard",
	factory: (require) => {
		var module = { exports: {} };
		var exports = module.exports;
		const React = require("react");
		const h = React.createElement;

		const inject = ["slots", "locale"];

		function DashboardPanel() {
			return h(
				"div",
				{ style: { display: "flex", flexDirection: "column", gap: "10px", padding: "4px 0" } },
				h(
					"div",
					{ style: { fontSize: "13px", color: "var(--dsw-alias-label-secondary)", lineHeight: "20px" } },
					"蓝色大肥鱼 · 只读仪表盘（需要本机 http://127.0.0.1:8090 在运行）"
				),
				h("iframe", {
					src: "http://127.0.0.1:8090",
					title: "蓝色大肥鱼仪表盘",
					style: {
						width: "100%",
						height: "72vh",
						border: "1px solid var(--dsw-alias-border-l2)",
						borderRadius: "12px",
						background: "#0d1020"
					}
				})
			);
		}

		function apply(ctx) {
			ctx.slots.inject("settings.general.item", () =>
				ctx.slots.register(
					{
						name: "settings.general.item",
						id: "dafeiyu-dashboard",
						order: 80
					},
					DashboardPanel
				)
			);
		}

		exports.apply = apply;
		exports.inject = inject;
		return module.exports;
	}
});
