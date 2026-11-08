import pstats, sys
s = pstats.Stats(sys.argv[1])
s.sort_stats("cumulative").print_stats(int(sys.argv[2]) if len(sys.argv) > 2 else 45)
