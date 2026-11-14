"""仅供隔离入口将真实Python生成的v4交给TypeScript，不读用户配置。"""
import json

from backend.tests.macro.test_enterprise_distribution import distribution_inputs, result


if __name__=='__main__':
    print(json.dumps({
        'normal':result(),
        'zeroBase':result(distribution_inputs(old=(0,0))),
        'overflow':result(distribution_inputs(('1e300',0),('1e-290',0))),
        'subnormal':result(distribution_inputs(('5e-316',0),('8e-316',0))),
    },allow_nan=False))
