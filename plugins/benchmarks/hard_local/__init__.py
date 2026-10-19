"""Small multi-rule local tasks sharing the deterministic scoring contract."""

from plugins.benchmarks.toy_tasks import ToyBenchmark


class HardLocalBenchmark(ToyBenchmark):
    benchmark_id = "hard_local"
    resource_package = __package__
