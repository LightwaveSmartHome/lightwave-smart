products = {
    "L5*": {
        "channel_zero_position": "left"
    }
}


def get_product(product_code):
    if product_code in products:
        return products[product_code]
    else:
        product_type = product_code[:1] + '*'
        if product_type in products:
            return products[product_type]
