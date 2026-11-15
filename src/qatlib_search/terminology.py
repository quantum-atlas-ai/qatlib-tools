"""Chinese vocabulary bridge for source discovery; no metadata/index enrichment."""
import re

CJK_RUN_RE = re.compile(r"[\u3400-\u9fff\u3000-\u303f\uff00-\uffef]+")

# Curated cross-lingual terminology bridge for CJK queries (longest match wins). These are
# vocabulary pointers into the library's English/Lean identifier space, not synonyms.
CJK_TERM_TABLE = {
    "振幅放大": ["amplitude", "amplification"],
    "幅度放大": ["amplitude", "amplification"],
    "振幅估计": ["amplitude", "estimation"],
    "二次加速": ["quadratic", "speedup"],
    "量子行走": ["quantum", "walk"],
    "量子游走": ["quantum", "walk"],
    "马尔可夫": ["markov"],
    "相位估计": ["phase", "estimation"],
    "线性系统": ["linear", "system"],
    "线性组合": ["linear", "combination"],
    "蒙特卡洛": ["monte", "carlo"],
    "奇异值": ["singular", "value"],
    "连通性": ["connectivity", "irreducible", "mixing", "spectral"],
    "行走": ["walk"],
    "搜索": ["search"],
    "计数": ["counting"],
    "查询": ["query", "oracle"],
    "测量": ["measurement"],
    "影子": ["shadow"],
    "哈密顿": ["hamiltonian"],
    "模拟": ["simulation"],
    "估计": ["estimation"],
    "放大": ["amplification"],
    "噪声": ["noise", "channel"],
    "信道": ["channel"],
    "混合态": ["mixed", "state"],
    "纯态": ["pure", "state"],
    "概率": ["probability"],
    "加速": ["speedup"],
    "下界": ["lower", "bound"],
    "自適應": ["adaptive"],
    "自适应": ["adaptive"],
    "反射": ["reflection"],
    "相位": ["phase"],
    "量子": ["quantum"],
    "图": ["graph", "walk", "markov"],
    "酉": ["unitary"],
    "矩阵": ["matrix"],
    "期望": ["expectation", "mean"],
}

def cjk_bridge(text: str) -> tuple[list[str], list[str]]:
    """Map CJK runs to English vocabulary through CJK_TERM_TABLE (longest match first).

    Returns (english_tokens, untranslated_cjk_runs)."""
    tokens: list[str] = []
    remaining = text
    for key in sorted(CJK_TERM_TABLE, key=len, reverse=True):
        while key in remaining:
            tokens.extend(CJK_TERM_TABLE[key])
            remaining = remaining.replace(key, " ", 1)
    untranslated = [run for run in CJK_RUN_RE.findall(remaining) if run.strip()]
    return tokens, untranslated
