import { getChartTheme } from "../chart-theme";

describe("chart theme gain/loss colors", () => {
  const originalLang = document.documentElement.lang;

  afterEach(() => {
    document.documentElement.lang = originalLang;
  });

  it("uses red-up/green-down in Chinese and reverses the colors elsewhere", () => {
    document.documentElement.lang = "zh-CN";
    const chinese = getChartTheme();
    document.documentElement.lang = "en";
    const international = getChartTheme();

    expect(chinese.upColor).toBe(international.downColor);
    expect(chinese.downColor).toBe(international.upColor);
    expect(chinese.upColor).not.toBe(chinese.downColor);
    expect(chinese.volumeUp).toBe(`${chinese.upColor}66`);
    expect(chinese.volumeDown).toBe(`${chinese.downColor}66`);
    expect(international.volumeUp).toBe(`${international.upColor}66`);
    expect(international.volumeDown).toBe(`${international.downColor}66`);
  });
});
