import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from src.utils import log_time, random_sleep

NEXT_PAGE_SELECTOR = (
    "button[class*='search-pagination-arrow-container']"
    ":has([class*='search-pagination-arrow-right'])"
    ":not([disabled])"
)
NEXT_PAGE_SELECTORS = (
    NEXT_PAGE_SELECTOR,
    "li[class*='pagination-next'] button",
    "li[class*='pagination-next'] [role='button']",
    "li[class*='pagination-next']",
    "li[title*='下一页'] button",
    "li[title*='Next'] button",
    "button:has([class*='anticon-right'])",
    "[role='button']:has([class*='anticon-right'])",
    "button:has-text('›')",
    "[role='button']:has-text('›')",
    "button:has-text('>')",
    "[role='button']:has-text('>')",
    "button[class*='search-pagination-arrow-container']"
    ":has([class*='search-pagination-arrow-right'])",
    "[class*='search-pagination-arrow-container']"
    ":has([class*='search-pagination-arrow-right'])",
    "button[aria-label*='下一页']",
    "[role='button'][aria-label*='下一页']",
    "button[aria-label*='Next']",
    "[role='button'][aria-label*='Next']",
    "button:has-text('下一页')",
    "[role='button']:has-text('下一页')",
    "li[class*='next'] button",
    "li[class*='next'] [role='button']",
)
PAGE_NUMBER_SELECTORS = (
    "li[title='{page_num}']",
    "li[class*='pagination-item'][title='{page_num}']",
    "li[class*='pagination-item-{page_num}']",
    "[class*='pagination'] button:has-text('{page_num}')",
    "[class*='Pagination'] button:has-text('{page_num}')",
    "[class*='pagination'] [role='button']:has-text('{page_num}')",
    "[class*='Pagination'] [role='button']:has-text('{page_num}')",
    "button:has-text('{page_num}')",
    "[role='button']:has-text('{page_num}')",
)
SEARCH_RESULTS_API_FRAGMENT = "/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/"
PAGE_REQUEST_TIMEOUT_MS = 20_000
PAGE_CLICK_TIMEOUT_MS = 10_000
PAGE_RETRY_DELAY_SECONDS = 5
PAGE_RETRY_COUNT = 2
PAGE_CLICK_SLEEP_MIN_SECONDS = 2
PAGE_CLICK_SLEEP_MAX_SECONDS = 5
PAGINATION_RENDER_WAIT_MS = 800


@dataclass(frozen=True)
class PageAdvanceResult:
    advanced: bool
    response: Optional[Any] = None
    stop_reason: Optional[str] = None
    diagnostics: Optional[str] = None


def is_search_results_response(
    response: Any,
    api_url_fragment: str = SEARCH_RESULTS_API_FRAGMENT,
) -> bool:
    request = getattr(response, "request", None)
    request_method = getattr(request, "method", None)
    response_url = getattr(response, "url", "")
    return api_url_fragment in response_url and request_method == "POST"


async def _maybe_call(locator: Any, method_name: str, default: Any = None) -> Any:
    method = getattr(locator, method_name, None)
    if method is None:
        return default
    try:
        return await method()
    except Exception:
        return default


async def _locator_attr(locator: Any, attr_name: str) -> Optional[str]:
    get_attribute = getattr(locator, "get_attribute", None)
    if get_attribute is None:
        return None
    try:
        return await get_attribute(attr_name)
    except Exception:
        return None


async def _is_usable_next_button(locator: Any) -> bool:
    is_visible = await _maybe_call(locator, "is_visible", True)
    if is_visible is False:
        return False

    if await _locator_attr(locator, "disabled") is not None:
        return False

    aria_disabled = await _locator_attr(locator, "aria-disabled")
    if str(aria_disabled).lower() == "true":
        return False

    class_name = await _locator_attr(locator, "class") or ""
    if "disabled" in str(class_name).lower():
        return False

    is_enabled = await _maybe_call(locator, "is_enabled", True)
    return is_enabled is not False


async def _has_exact_text(locator: Any, expected: str) -> bool:
    inner_text = getattr(locator, "inner_text", None)
    if inner_text is None:
        return True
    try:
        text = await inner_text()
    except Exception:
        return True
    return " ".join(str(text).split()) == expected


async def _prepare_pagination(page: Any) -> None:
    evaluate = getattr(page, "evaluate", None)
    if evaluate is not None:
        try:
            await evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass

    wait_for_timeout = getattr(page, "wait_for_timeout", None)
    if wait_for_timeout is not None:
        try:
            await wait_for_timeout(PAGINATION_RENDER_WAIT_MS)
        except Exception:
            pass


async def _find_usable_locator(page: Any, selectors: tuple[str, ...]) -> Optional[Any]:
    for selector in selectors:
        candidate = page.locator(selector).first
        if not await candidate.count():
            continue
        if await _is_usable_next_button(candidate):
            return candidate
    return None


async def _find_page_number_button(page: Any, page_num: int) -> Optional[Any]:
    page_text = str(page_num)
    selectors = tuple(
        selector.format(page_num=page_text) for selector in PAGE_NUMBER_SELECTORS
    )
    for selector in selectors:
        candidate = page.locator(selector).first
        if not await candidate.count():
            continue
        if not await _is_usable_next_button(candidate):
            continue
        if not await _has_exact_text(candidate, page_text):
            continue
        return candidate
    return None


async def _pagination_diagnostics(page: Any) -> str:
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return "pagination diagnostics unavailable"
    try:
        return await evaluate(
            """
            () => {
              const nodes = Array.from(document.querySelectorAll(
                "button,[role='button'],li[class*='next'],[class*='pagination'],[class*='Pagination']"
              ));
              return nodes.slice(0, 20).map((node) => {
                const text = (node.innerText || node.textContent || "").trim().replace(/\\s+/g, " ");
                const cls = String(node.className || "");
                return {
                  tag: node.tagName,
                  text: text.slice(0, 40),
                  className: cls.slice(0, 120),
                  disabled: Boolean(node.disabled),
                  ariaDisabled: node.getAttribute("aria-disabled"),
                  ariaLabel: node.getAttribute("aria-label"),
                  visible: Boolean(node.offsetWidth || node.offsetHeight || node.getClientRects().length)
                };
              }).map((item) => JSON.stringify(item)).join(" | ");
            }
            """
        ) or "no pagination-like nodes"
    except Exception as exc:
        return f"pagination diagnostics failed: {exc}"


async def advance_search_page(
    *,
    page: Any,
    page_num: int,
    logger: Callable[[str], None] = log_time,
    wait_after_click: Callable[[float, float], Awaitable[None]] = random_sleep,
    retry_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    max_retries: int = PAGE_RETRY_COUNT,
) -> PageAdvanceResult:
    await _prepare_pagination(page)
    page_button = await _find_page_number_button(page, page_num)
    advance_button = page_button or await _find_usable_locator(
        page, NEXT_PAGE_SELECTORS
    )
    if advance_button is None:
        diagnostics = await _pagination_diagnostics(page)
        logger(f"未找到可用的第 {page_num} 页页码或下一页按钮，停止翻页。")
        logger(f"翻页诊断: {diagnostics}")
        return PageAdvanceResult(
            advanced=False,
            stop_reason="no_next_button",
            diagnostics=diagnostics,
        )

    for retry_index in range(max_retries):
        try:
            await advance_button.scroll_into_view_if_needed()
            async with page.expect_response(
                is_search_results_response,
                timeout=PAGE_REQUEST_TIMEOUT_MS,
            ) as response_info:
                try:
                    await advance_button.click(timeout=PAGE_CLICK_TIMEOUT_MS)
                except PlaywrightTimeoutError:
                    logger(f"第 {page_num} 页翻页按钮点击超时，停止翻页。")
                    return PageAdvanceResult(
                        advanced=False,
                        stop_reason="click_timeout",
                    )
            await wait_after_click(
                PAGE_CLICK_SLEEP_MIN_SECONDS,
                PAGE_CLICK_SLEEP_MAX_SECONDS,
            )
            return PageAdvanceResult(
                advanced=True,
                response=await response_info.value,
            )
        except PlaywrightTimeoutError:
            if retry_index < max_retries - 1:
                logger(
                    f"等待第 {page_num} 页搜索响应超时，"
                    f"{PAGE_RETRY_DELAY_SECONDS}秒后重试..."
                )
                await retry_sleep(PAGE_RETRY_DELAY_SECONDS)
                continue

            logger(f"等待第 {page_num} 页搜索响应超时 {max_retries} 次，停止翻页。")
            return PageAdvanceResult(advanced=False, stop_reason="response_timeout")

    return PageAdvanceResult(advanced=False, stop_reason="unknown")
