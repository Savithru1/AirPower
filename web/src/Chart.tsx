import { useEffect, useRef } from "react";
import * as echarts from "echarts";

// Thin ECharts wrapper. Colours are read from the CSS tokens so charts follow the theme.
export function css(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

export function Chart({ option, height = 280 }: { option: object; height?: number }) {
  const el = useRef<HTMLDivElement>(null);
  const inst = useRef<echarts.ECharts | null>(null);
  useEffect(() => {
    if (!el.current) return;
    inst.current = echarts.init(el.current, undefined, { renderer: "svg" });
    const ro = new ResizeObserver(() => inst.current?.resize());
    ro.observe(el.current);
    return () => { ro.disconnect(); inst.current?.dispose(); };
  }, []);
  useEffect(() => {
    const ink2 = css("--ink-2"), line = css("--line");
    inst.current?.setOption({
      textStyle: { color: ink2, fontFamily: "system-ui, sans-serif" },
      grid: { left: 44, right: 16, top: 28, bottom: 32 },
      ...(option as echarts.EChartsOption),
      xAxis: { axisLine: { lineStyle: { color: line } }, axisLabel: { color: ink2 }, ...((option as echarts.EChartsOption).xAxis as object) },
      yAxis: { splitLine: { lineStyle: { color: line } }, axisLabel: { color: ink2 }, ...((option as echarts.EChartsOption).yAxis as object) },
    } as echarts.EChartsOption, true);
  }, [option]);
  return <div ref={el} style={{ width: "100%", height }} role="img" aria-label="chart" />;
}
