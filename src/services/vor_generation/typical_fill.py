"""Public entry point for the current pair of typical VORs and their comparison."""

OUTPUT_FILENAME = "Типовая_ВОР_сверка.xlsx"
GENERATED_FILENAME = "Сформированный_ВОР_типовой.xlsx"
CUSTOMER_FILENAME = "ВОР_заказчика_типовой.xlsx"


def build(run_dir, output_path=None, template_path=None, *, session=None, llm=None, force=False):
    from . import session_typical
    return session_typical.build(run_dir, output_path=output_path, template_path=template_path,
                                 session=session, llm=llm, force=force)
