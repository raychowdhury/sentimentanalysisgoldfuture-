"""
Stock sentiment + bias engine for a fixed S&P 500 top-20 universe.

Parallel to the gold pipeline. Reuses news/sentiment primitives but keeps
gold-specific scoring (dxy/yield/cot/vix) out of the stock signal path.
"""
