import argparse
import importlib.util
import sys
import types

from kfp import compiler, dsl


def main(argv=None):
    parser = argparse.ArgumentParser(description="Compile a minipipe pipeline file with the Kubeflow Pipelines SDK.")
    parser.add_argument("file")
    parser.add_argument("output")
    parser.add_argument("--pipeline", default="train_pipeline")
    args = parser.parse_args(argv)
    sys.modules["minipipe"] = types.SimpleNamespace(dsl=dsl)
    spec = importlib.util.spec_from_file_location("pipeline_for_kfp", args.file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    compiler.Compiler().compile(getattr(module, args.pipeline), args.output)
    print(f"kfp compiled {args.pipeline} -> {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
