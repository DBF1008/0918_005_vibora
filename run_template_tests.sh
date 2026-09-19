#!/usr/bin/env bash
#
# 手动运行模板子系统的单元测试。
#
# 覆盖范围:
#   - vibora/templates/engine.py   (事务性模板加载 / 增量编译)
#   - vibora/templates/loader.py   (debug 模式增量重编译)
#   - vibora/templates/compilers/cython.py (源码映射 / 异常回溯)
#
# 用法:
#   ./run_template_tests.sh          运行全部模板测试
#   ./run_template_tests.sh -v       详细输出
#
set -euo pipefail
cd "$(dirname "$0")"

exec python3 -m unittest \
    tests.templates.engine \
    tests.templates.loader \
    tests.templates.cython \
    tests.templates.render \
    tests.templates.exceptions \
    tests.templates.nodes \
    "$@"
