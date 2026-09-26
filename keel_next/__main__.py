"""Run local capability checks, integration demos or an offline pipeline benchmark."""
import argparse
import json
import sys
from .workflow import demo, doctor


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['doctor','demo','benchmark','qualify','evolve'])
    parser.add_argument('--home',required=True)
    parser.add_argument('--boards',type=int,default=3)
    parser.add_argument('--jobs-per-board',type=int,default=100)
    parser.add_argument('--max-new',type=int,default=150)
    args=parser.parse_args(argv)
    try:
        if args.command=='evolve':
            from keel_evolve.demo import run
            result=run(args.home)
        elif args.command=='qualify':
            from .qualification import qualify
            result=qualify(args.home)
        elif args.command=='benchmark':
            from .benchmark import run_benchmark
            result=run_benchmark(args.home,boards=args.boards,
                                 jobs_per_board=args.jobs_per_board,max_new=args.max_new)
        else:
            result=(demo if args.command=='demo' else doctor)(args.home)
        print(json.dumps(result,indent=2,sort_keys=True,allow_nan=False))
        return 3 if result.get('status') in ('DEMO_FAILED','BENCHMARK_FAILED','QUALIFICATION_FAILED','EVOLUTION_DEMO_FAILED') else 0
    except (OSError,ValueError,TypeError,KeyError,RuntimeError):
        print('keel-next: blocked input or host; demo needs a new directory under an existing owner-controlled parent; writable non-sticky ancestors are forbidden',file=sys.stderr)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
