from decimal import Decimal


def round_decimal(target_dict: dict):
    target_dict = {
        key: (
            Decimal(value).quantize(Decimal("0.001"))
            if Decimal(value).as_tuple().exponent < -3
            else value
        )
        for key, value in target_dict.items()
    }
    return target_dict
