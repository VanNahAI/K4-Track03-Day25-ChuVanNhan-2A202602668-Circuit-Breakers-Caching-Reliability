# Lab 25B — Report

**Họ tên / nhóm:** Chu Van Nhan
**MSSV:** 2A202602668
**Commit:** ……  
**Điểm tự động (`make grade`):** 84 / 88

4 điểm còn lại là SLO trên bộ dữ liệu ẩn, giảng viên chấm. Trên máy này `python scripts/grade.py` cho 84/88: unit test 42/42, 6/6 băng Game Day, workflow, SLO public, băng C1. Số liệu bên dưới lấy từ `reports/gameday/*.json`, `reports/workflows.json`, `reports/slo.json`, `reports/cache/*.json` và CSV `reports/gameday/naive_G2_overload_storm_timeseries.csv` (seed 25).

## 1. Kết quả Game Day (naive vs của bạn)

| Băng | Client | availability | p95 (s) | amplification | retry_ratio | cost/turn | recovery (s) | PASS? |
|---|---|---:|---:|---:|---:|---:|---:|:---:|
| G1 | naive | 0.7194 | 7.157 | 7.738 | 6.738 | 2.163 | 4.0 | FAIL |
| G1 | yours | 1.0000 | 4.776 | 1.062 | 0.002 | 2.101 | 0.0 | PASS |
| G2 | naive | 0.4560 | 15.034 | 9.904 | 8.904 | 4.627 | 155.0 | FAIL |
| G2 | yours | 1.0000 | 11.911 | 1.059 | 0.008 | 2.161 | 0.0 | PASS |
| G3 | naive | 1.0000 | 4.768 | 1.164 | 0.164 | 2.328 | 0.0 | FAIL |
| G3 | yours | 1.0000 | 4.738 | 1.058 | 0.034 | 2.141 | 0.0 | PASS |
| G4 | naive | 1.0000 | 13.764 | 1.004 | 0.004 | 2.008 | 0.0 | FAIL |
| G4 | yours | 1.0000 | 5.562 | 1.015 | 0.002 | 2.072 | 0.0 | PASS |
| G5 | naive | 0.3699 | 5.359 | 17.030 | 16.030 | 2.124 | — | FAIL |
| G5 | yours | 0.8179 | 5.477 | 1.003 | 0.002 | 2.504 | — | PASS |
| G6 | naive | 0.5170 | 4.836 | 12.344 | 11.344 | 2.009 | 0.0 | FAIL |
| G6 | yours | 1.0000 | 5.961 | 1.055 | 0.018 | 2.196 | 0.0 | PASS |

G1 naive còn 36.027 lần vi phạm `retry-after`. G5 naive retry 9.149 lỗi 400 (`client_error_retries`). Client này: 0 vi phạm `retry-after`, 0 retry lỗi 400, 0 route không tương thích trên cả 6 băng.

## 2. Cấu hình client và lý do

| Tham số | Giá trị | Vì sao (dựa trên băng nào, số liệu nào) |
|---|---:|---|
| attempt_timeout_s | 8 | G6: `alpha-eu` treo. Timeout 30 s làm availability G6 tụt từ 1.0000 xuống 0.9488 (dưới ngưỡng 0.95). Ở 8 s, G6 availability = 1.0000, p95 = 5.961 s. G4 vẫn trả lời (~6,2 s) nên breaker nhìn thấy slow call thay vì nuốt cả lượt. |
| max_attempts_per_provider | 2 | Một lần retry cùng provider rồi fail over. G3 `retry_ratio` = 0.034 (ngưỡng 0.10), amplification = 1.058 (ngưỡng 1.6). |
| backoff_base_s / cap_s | 0.25 / 4 | Full jitter, trần 4 s. Rate limit không dùng backoff này: cooldown = `now + retry_after`, nên G1 có 0 vi phạm `retry-after` (naive: 36.027). |
| retry_budget_ratio | 0.1 | Trần retry ~10% request. G3 `retry_ratio` thực tế 0.034. Khi breaker bị vô hiệu (mục 3), budget giữ amplification G2 ở 1.378 thay vì 1.675. |
| breaker.window_size / min_calls | 20 / 10 | Không mở mạch vì vài lỗi lẻ. Brown-out G3 chỉ 30% nên không đủ 50% trên cửa sổ; từng lỗi 500 vẫn fail over. G2/G6 đủ timeout và 529 để mở mạch trong vài giây. |
| breaker.failure_rate_threshold | 0.5 | Đa số mẫu trong cửa sổ phải hỏng thì mới mở. G3 không bị cắt hết traffic khỏi alpha; G2 (529 + timeout) và G6 (503) thì có. |
| breaker.slow_call_s | 5 | G4 cộng thêm 5 s, alpha ~6,2 s ≥ 5 s. Naive p95 = 13.764 s; client này p95 = 5.562 s và `primary_share_after_restore` = 1.0 sau khi alpha nhanh lại. |
| breaker.open_duration_s | 10 | G2 `recovery_s` = 0 (ngưỡng 30 s) và `primary_share_after_restore` = 1.0. Probe chạy lại sau 10 s, trước cửa sổ đo share (restore + 60 s). |
| breaker.half_open_max_probes | 2 | Giới hạn probe đồng thời khi alpha vừa hồi. G2 amplification = 1.059, không có đợt dồn lại lúc HALF-OPEN. |

Sơ đồ quyết định của `on_error`:

```
CLIENT_ERROR      release permit          GiveUp("client_error")     không retry, không failover
QUOTA_EXHAUSTED   release                 cooldown 300 s             failover ngay
RATE_LIMITED      release                 cooldown = now+retry_after failover ngay
OVERLOADED        on_failure (tính vào breaker)
SERVER_ERROR      on_failure
TIMEOUT           on_failure
    nếu tries < 2 VÀ còn token budget VÀ delay vừa deadline
        -> retry cùng provider, delay = full jitter
    ngược lại
        -> failover, delay = 0
Hết ladder        chờ cooldown rate-limit ngắn nhất nếu còn <= 5 s,
                  breaker không OPEN, và còn kịp deadline
                  ngược lại GiveUp("no_provider_available")

ladder("tool")    bỏ provider không supports_tools (gamma-mini)
```

## 3. Ba thí nghiệm (mỗi cái 3–5 câu + số)

1. **Retry budget là lớp phòng thủ cuối.** Breaker bị làm hỏng (`min_calls: 1000000000`) và `max_attempts_per_provider: 5`.

   | Băng | Budget | availability | amplification | retry_ratio | recovery (s) |
   |---|---|---:|---:|---:|---:|
   | G2 | bật (ratio 0.1) | 0.9983 | 1.378 | 0.058 | 0.0 |
   | G2 | tắt (ratio 100, token không giới hạn) | 0.9666 | 1.675 | 0.495 | 25.0 |
   | G3 | bật | 1.0000 | 1.136 | 0.068 | 0.0 |
   | G3 | tắt | 1.0000 | 1.162 | 0.159 | 0.0 |

   Khi breaker không mở, tắt budget trên G2 đẩy `retry_ratio` từ 0.058 lên 0.495 và amplification từ 1.378 lên 1.675 (vượt ngưỡng 1.6), recovery từ 0 s lên 25 s, availability từ 0.9983 xuống 0.9666. G3 cũng lộ: `retry_ratio` 0.068 so với 0.159, trong khi ngưỡng băng là 0.10. Với breaker đang chạy đúng, G2 amplification chỉ 1.059 và G3 `retry_ratio` chỉ 0.034 — budget hầu như không phải thứ đang cứu băng. Vẫn cần cả hai: breaker cắt provider hỏng, budget chặn cấu hình breaker sai hoặc một loại lỗi mà cửa sổ chưa kịp mở.

2. **`attempt_timeout_s: 30`.**

   | Timeout | Băng | availability | p95 (s) |
   |---|---|---:|---:|
   | 8 s | G4 | 1.0000 | 5.562 |
   | 30 s | G4 | 1.0000 | 5.608 |
   | 8 s | G6 | 1.0000 | 5.961 |
   | 30 s | G6 | 0.9488 | 5.410 |

   G4 gần như không đổi: alpha vẫn trả lời trong ~6 s, chậm hơn ngưỡng 5 s, breaker mở vì slow call. p95 của lượt thành công không bị timeout 30 s kéo dài. G6 thì `alpha-eu` treo đến khi client bỏ cuộc. Timeout 30 s giữ lượt user trên provider đã chết đủ lâu để availability rơi xuống 0.9488, dưới ngưỡng 0.95. Timeout nên đặt theo latency bình thường của provider (alpha median 1,2 s, đuôi chậm của G4 ~6 s) cộng một khoảng nhỏ, đủ để phân biệt "chậm" với "treo", và phải nhỏ hơn nhiều so với deadline 30 s của cả lượt.

3. **Bỏ lọc tool calling khỏi `ladder()`.** G6: `incompatible_routes` từ 0 lên 257, availability từ 1.0000 xuống 0.8922. 257 lượt tool bị gửi sang `gamma-mini`, model không gọi được tool hoàn tiền. Với người dùng, đó là 257 cuộc hội thoại bị trả lời sai hoặc bỏ dở vì agent không tra được đơn — rẻ hơn mỗi call (0,2 so với 1,0) nhưng không làm được việc cần làm. Lọc theo `supports_tools` giữ những call đó trên alpha / alpha-eu / beta.

## 4. Workflow hoàn tiền

| Executor | completion | crashes | duplicate refunds | duplicate emails | lost refunds | overpaid | wasted LLM / crash |
|---|---:|---:|---:|---:|---:|---:|---:|
| naive | 1.0000 | 81 | 128 | 81 | 0 | 1.574.200 cent | 1.123 |
| yours | 1.0000 | 66 | 0 | 0 | 0 | 0 | 0.303 |

Chỉ idempotency key thì chưa đủ. Key giúp payment và email bỏ qua lần gọi lại cùng một side effect, nhưng classify / lookup / summarize không có key. Worker chết sau classify thì worker mới gọi LLM lại từ đầu. `wasted_llm_per_crash` của naive là 1.123 vì không có checkpoint. Chỉ checkpoint cũng chưa đủ: `ServiceTimeout` nghĩa là payment có thể đã trừ tiền nhưng response mất, trước khi mình kịp ghi checkpoint. Không có key ổn định, lần chạy lại sẽ tạo một refund thứ hai. Cần cả hai: key `session_id:step` để payment khử trùng, checkpoint ghi **sau** khi step trả về để worker mới bỏ qua bước đã xong. `wasted_llm_per_crash` của executor này là 0.303, 0 refund trùng, 0 email trùng, 0 refund mất, 500/500 hoàn tất.

Ghi checkpoint **trước** khi gọi payment thì một crash nằm giữa "đã ghi xong" và "tiền chưa đi" khiến worker sau tưởng bước đã xong và bỏ qua. Khách không được hoàn tiền (`lost_refunds`), trong khi store vẫn nói là done.

## 5. Alerting & error budget

| Sự cố | Mong đợi | Naive alerter | Burn-rate alerter | Độ trễ phát hiện |
|---|---|---|---|---:|
| checkout_bug | page | page | page (`fast_burn_2pct_1h`) | naive 1 phút, burn-rate 27 phút (hạn 35) |
| provider_outage | page | page | page (`fast_burn_2pct_1h`) | naive 0 phút, burn-rate 4 phút (hạn 8) |
| deploy_blip | none | page (false) | không page | — |
| slow_leak | ticket | không ticket, không page | ticket (`budget_leak_10pct_3d`) | 1337 phút (hạn 1800), `paged_instead` = false |
| degraded_evening | page | page | page (`slow_burn_5pct_6h`) | naive 214 phút, burn-rate 291 phút (hạn 330) |
| night_spike | none | page (false) | không page | — |
| small_bump | none | page (false) | không page | — |

Naive: 7 page, 0 ticket, 3 false page. Burn-rate: 3 page, 1 ticket, 0 false page. Public SLO PASS.

Error budget 30 ngày (`reports/slo.json`, SLO 99,5%): 25.901.755 request, cho phép 129.508,8 request lỗi, thực tế 55.618 lỗi. Availability 0,997853. Đã tiêu **42,95%**, còn **57,05%**. `freeze_releases` = false — còn hơn một nửa budget, chưa phải đóng băng release.

`small_bump` là 20 phút ở 12% lỗi, khoảng 1% budget tháng, và đã tự hết. Luật page nhanh cần **cả** cửa sổ 1 giờ lẫn 5 phút đạt 14,4×. 20 phút lỗi bị pha loãng bởi ~40 phút khỏe trong cửa sổ 1 giờ: trung bình khoảng (20 × 0,12) / 60 ≈ 4% lỗi, burn ≈ 4% / 0,5% = 8×, dưới 14,4×. Cửa sổ 6 giờ còn thấp hơn 6×. Đánh thức on-call lúc 11 giờ trưa cho một blip đã kết thúc, chưa đốt đáng kể budget tháng, là nhiễu. Ticket 3 ngày cũng không nhắm vào 20 phút.

## 6. Postmortem — Băng G2 với client naive (blameless)

Hình: `reports/gameday/G2_overload_storm.png`. Số trong timeline lấy từ `reports/gameday/naive_G2_overload_storm_timeseries.csv` và `reports/gameday/naive.json`.

**Tóm tắt:** Lúc t = 120, alpha mất 75% GPU trong 3 phút. NaiveClient retry tối đa 36 lần, delay cố định 0,2 s, không jitter, không tôn trọng `retry-after`, không breaker, không fallback, nên toàn bộ retry dồn lại alpha. Provider hồi phục lúc t = 300 nhưng hàng đợi vẫn nghẹt đến t = 455; 1.285 / 2.362 lượt user thất bại (availability 0,456).

**Ảnh hưởng:** 2.362 lượt, 1.077 thành công, 1.285 thất bại. Kéo dài từ t = 120 đến khi cửa sổ 30 s đạt 95% thành công ở t = 455, tức 335 s, trong đó 155 s là sau khi GPU đã trả lại. 34.475 attempt cho 3.481 logical call (amplification 9,904), tất cả trên alpha. 26.298 lỗi `rate_limit_exceeded`, 28.381 vi phạm `retry-after`, 3.194 `overloaded_error`, 2.737 `client_timeout`. Chi phí 4,627 / lượt tốt, so với 2,161 của client có breaker trên cùng băng.

Error budget tháng, giả sử tháng chỉ có sự cố này và traffic nền 4 lượt/s: tháng có 4 × 86.400 × 30 = 10.368.000 lượt, ngân sách 0,5% là 51.840 lượt lỗi. 1.285 lượt thất bại tiêu **2,48%** budget tháng — chưa hết budget, nhưng trong đoạn t = 180–455 có 1.061 call kết thúc và chỉ 8 call thành công (tỷ lệ lỗi 99,2%), burn ≈ 0,992 / 0,005 = **198×** mức SLO. Đó là page, không phải một blip.

**Timeline** (giây ảo):

| t | Sự kiện | Bằng chứng (số) |
|---:|---|---|
| 0–120 | Bình thường, mọi call vào alpha thành công | 895/895 call ok, 905 attempt alpha, 7,5 attempt/s |
| 120 | Alpha mất 75% năng lực | Từ đây tỷ lệ ok rơi. t = 119: 7/7 ok, 9 attempt. t = 121: 2/2 ok, 7 attempt |
| 143 | Đỉnh retry storm | 254 attempt/s trên alpha; call hoàn thành trong giây đó = 0 (kết quả trả về trễ) |
| 120–180 | Bắt đầu sập | 51/222 call ok (tỷ lệ 0,23), 6.943 attempt, 115,7 attempt/s |
| 180–300 | Sập hoàn toàn trong lúc GPU vẫn mất | 0/507 call ok, 17.759 attempt, 148 attempt/s. Tenant alpha chỉ 20 rps |
| 300 | Alpha hồi phục hoàn toàn (capacity trở lại 100%) | Giây t = 300 vẫn 0/5 ok và 115 attempt |
| 300–360 | GPU đã về, hệ thống vẫn 0% thành công | 0/169 call ok, 1.178 attempt, 19,6 attempt/s |
| 360–455 | Hàng đợi retry tự nuôi | 8/385 call ok (tỷ lệ 0,021), 6.447 attempt, 67,9 attempt/s |
| 455 | Cửa sổ 30 s đầu tiên đạt ≥ 95% call thành công | recovery = 455 − 300 = 155 s. Giây 455: 4/5 ok, 13 attempt |
| 480 | Ổn định | 11/11 ok, 8 attempt/s |
| 520–600 | Trở lại nền | 584/584 ok, 7,3 attempt/s |

**Trigger và root cause.** Trigger là alpha mất 75% GPU từ t = 120 đến t = 300 (529 và hàng đợi dài). Root cause là ba tầng retry chồng nhau (app × gateway × SDK, tối đa 36 attempt/call), delay cố định, không phân loại lỗi, không `retry-after`, không circuit breaker, không fallback. Vòng lặp tự duy trì: client timeout nhưng server vẫn xử lý request đó; retry tạo attempt mới; attempt mới bị 429 vì vượt 20 rps và lại bị retry ngay (28.381 vi phạm `retry-after`). Sau t = 300 năng lực GPU đã đủ cho ~7,5 attempt/s của traffic thật, nhưng 20–148 attempt/s từ retry giữ hàng đợi đầy. Request mà user đã bỏ vẫn chiếm slot. Đó là metastable failure: trạng thái nghẽn được chính lượng retry giữ lại sau khi nguyên nhân gốc đã hết.

**Điều gì giúp / điều gì làm tệ hơn.** Làm tệ hơn: delay 0,2 s không jitter (mọi client quay lại cùng lúc), retry cả 429 và 529, không có ngân sách retry, một provider duy nhất. Giúp, ở bản sửa: lỗi 429 chỉ cooldown rồi đổ sang alpha-eu / beta (G2 amplification 1,059, recovery 0 s, availability 1,0); timeout và 529 đi vào breaker; full jitter và retry budget chặn bão nếu breaker chưa kịp mở. Điểm còn đau của bản sửa nằm ở mục 7: p95 G2 vẫn 11,911 s vì attempt đã bay phải chờ hết timeout 8 s.

**Action items:**

| # | Hành động | Loại | Owner | Cách kiểm chứng |
|---|---|---|---|---|
| 1 | Một tầng retry, full jitter, tôn trọng `retry-after`, retry budget ratio 0,1 | prevent | đội client | G2 amplification ≤ 1,2 và `retry_after_violations` = 0 trên CI (`make gameday`) |
| 2 | Circuit breaker theo tỷ lệ lỗi + slow call, HALF-OPEN tối đa 2 probe | mitigate | đội client | G2 `recovery_s` ≤ 30 và `primary_share_after_restore` ≥ 0,8 |
| 3 | Page theo burn-rate 14,4× (1 h và 5 phút), không page theo ngưỡng lỗi 5 phút | detect | SRE | `make slo`: 0 false page trên `deploy_blip`, `night_spike`, `small_bump`; page `provider_outage` trong 8 phút |
| 4 | Dashboard attempt/s theo provider cạnh success ratio 10 s | detect | SRE | Trong G2 naive, attempt/s vượt 100 trong khi success ratio = 0 là tín hiệu; alert khi attempt/s > 3× nền trong 1 phút |

## 7. Một điểm yếu còn lại

Trên G2, client này đạt availability 1,0 và recovery 0 s, nhưng p95 của lượt thành công là **11,911 s** (p99 = 16,810 s). Có 190 `client_timeout`: call đã gửi lên alpha phải chờ hết `attempt_timeout_s` = 8 trước khi breaker kịp mở và đổ sang nơi khác. Một lượt gồm tool + chat nên hai lần chờ đó cộng lại. Hướng sửa: hạ timeout xuống sát ngưỡng slow-call (khoảng 4–5 s) sau khi cửa sổ đã có vài mẫu chậm, hoặc giới hạn số attempt đang bay trên mỗi provider để lượt mới fail over ngay khi hàng đợi alpha đã dài, thay vì mỗi lượt tự bỏ 8 s.

## 8. Cache (Part G)

Số từ `reports/cache/*.json` (seed 25, `similarity_threshold` 0,92, `ttl_s` 60, `max_stale_s` 400).

| Client | availability | faq_availability | cache_hit_ratio | stale_served | wrong_answers | outdated_answers | cost/turn | PASS? |
|---|---:|---:|---:|---:|---:|---:|---:|:---:|
| resilient (không cache) | 0.8373 | 0.8424 | 0.000 | 0 | 0 | 0 | 1.386 | FAIL |
| cached (của bạn) | 0.9124 | 0.9645 | 0.652 | 157 | 0 | 3 | 0.915 | PASS |

| Tham số `cache:` | Giá trị | Vì sao (số liệu nào) |
|---|---:|---|
| similarity_threshold | 0.92 | Paraphrase cùng ý có cosine thấp nhất 0,9507; cặp ý gần-mà-khác cao nhất 0,8731. 0,92 nằm giữa hai khoảng. Ở 0,80: `wrong_answers` = 175, toàn bộ `other_question`, `faq_availability` = 0,8605. Ở 0,92: wrong = 0, hit = 0,652, cost/turn = 0,915. Ở 0,97: wrong = 0 nhưng hit tụt còn 0,579 và cost/turn lên 0,965. |
| ttl_s | 60 | Webhook t = 150 chỉ invalidate `policy/refund`. Bảng phí ship đổi lúc t = 480 không có webhook, nên chỉ TTL cắt câu trả lời cũ. ttl 30: hit 0,592, outdated 2. ttl 60: hit 0,652, outdated 3 (ngưỡng 15). ttl 300: hit 0,731, outdated 95 — trượt băng. |
| max_stale_s | 400 | Outage mọi provider kéo dài 120 s (t = 240–360). `max_stale_s: 0` cho `faq_availability` = 0,8733 (dưới 0,93) và `stale_served` = 0. Với 400 s: faq = 0,9645, stale = 157, wrong = 0. |

Thí nghiệm:

1. **Ngưỡng.** C1 với threshold 0,80 / 0,92 / 0,97 (ttl = 60). 0,80: hit 0,659, cost/turn 0,978, **175** `wrong_answers`, reason duy nhất `other_question` — cache trả nhầm câu gần nghĩa (hoàn tiền *chính sách* với hoàn tiền *bao lâu*, phí ship nội thành với phí ship đi tỉnh, đổi size với đổi màu). 0,92: hit 0,652, wrong 0, cost/turn 0,915. 0,97: wrong 0, hit 0,579, cost/turn 0,965 vì paraphrase dưới 0,97 phải gọi provider lại. 0,92 giữ hit trên 0,50 mà không trộn intent.

2. **TTL.** ttl 30 / 60 / 300, threshold 0,92. Outdated = 2 / 3 / 95, hit = 0,592 / 0,652 / 0,731. Webhook không đụng `policy/shipping`: sự kiện im lặng lúc t = 480 không gọi `on_content_update`. Entry phí ship vẫn fresh cho đến hết TTL, và mỗi lần serve trong khoảng đó bị đếm `outdated_answers`. TTL là thứ duy nhất giới hạn khoảng đó. 60 s giữ outdated = 3, dưới trần 15, hit 0,652.

3. **Stale-if-error.** `max_stale_s: 0` (cùng ttl 60, threshold 0,92): `faq_availability` từ 0,9645 xuống **0,8733**, `stale_served` = 0. Trong 120 s mọi provider sập, câu FAQ không còn câu trả lời cũ để đưa. Với `max_stale_s` 400, 157 câu stale được phục vụ và faq vượt 0,93, wrong vẫn 0 vì lookup lại sau invalidation và sau khi đổi `prompt_version`.

Câu hỏi cá nhân ("Đơn #48213 của tôi đang ở đâu?") không được cache, kể cả cache theo từng user. Embedding của hai đơn chỉ lệch rất nhỏ (catalog đẩy cosine ~0,997 khi đổi mã đơn), nên "đơn của tôi" và "đơn của người khác" gần như trùng vector — một hit là lộ trạng thái đơn của user khác (`other_user`). Trạng thái đơn còn đổi trong vài phút, nhanh hơn mọi TTL hợp lý cho FAQ (`personal_cached`). Vì vậy `req.personal` bỏ qua cả `store` lẫn `lookup`.

Trong lúc mọi provider sập, dashboard user vẫn xanh vì FAQ được phục vụ từ cache (`faq_availability` 0,9645 trong khi resilient không cache chỉ 0,8424). On-call không được alert theo `faq_availability`. Alert theo tỷ lệ lỗi provider và trạng thái breaker (cả bốn provider OPEN / GiveUp), cộng với `stale_served` tăng trong khi attempt thành công bằng 0. Đó là sự cố đang bị cache che, không phải hệ thống khỏe.
