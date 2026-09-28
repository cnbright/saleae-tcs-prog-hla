"""Minimal runtime shim for adapter replay; never shipped as a saleae package."""
import importlib
import sys
import types


class Frame:
    def __init__(self, type, start_time, end_time, data=None):
        self.type, self.start_time, self.end_time, self.data = type, start_time, end_time, data or {}


class Choice:
    def __init__(self, choices, label=None):
        self.choices = choices


def load_hla(direction='Auto', display='Transactions'):
    analyzers = types.ModuleType('saleae.analyzers')
    analyzers.HighLevelAnalyzer = type('HighLevelAnalyzer', (), {})
    analyzers.AnalyzerFrame = Frame
    analyzers.ChoicesSetting = Choice
    sys.modules['saleae'] = types.ModuleType('saleae')
    sys.modules['saleae.analyzers'] = analyzers
    Hla = importlib.import_module('HighLevelAnalyzer').Hla
    obj = Hla()
    obj.direction, obj.display = direction, display
    return obj
