import * as echarts from "echarts/core";
import { CandlestickChart, LineChart, BarChart, HeatmapChart } from "echarts/charts";
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  MarkPointComponent,
  ToolboxComponent,
  MarkLineComponent,
  MarkAreaComponent,
  VisualMapComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";

echarts.use([
  CandlestickChart, LineChart, BarChart, HeatmapChart,
  GridComponent, TooltipComponent, LegendComponent,
  DataZoomComponent, MarkPointComponent,
  ToolboxComponent, MarkLineComponent, MarkAreaComponent,
  VisualMapComponent,
  CanvasRenderer,
]);

export const CHART_GROUP = "quant-charts";

const connectedGroups = new Set<string>();

export function connectCharts(group = CHART_GROUP) {
  if (!connectedGroups.has(group)) {
    echarts.connect(group);
    connectedGroups.add(group);
  }
}

export { echarts };
