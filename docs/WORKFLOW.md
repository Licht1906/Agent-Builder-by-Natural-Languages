# Quy trình làm việc — Agent Builder by Natural Languages

Tài liệu này dành cho cả 4 thành viên. Đọc hết một lần trước khi viết dòng code đầu tiên.

## 0. Đọc gì trước

| Tài liệu | Nội dung |
|---|---|
| [`TOPIC.md`](../TOPIC.md) | Đề bài gốc BTC giao |
| [`docs/specs/agent-builder.md`](specs/agent-builder.md) | **Spec (PRD) đầy đủ** — cũng là GitHub issue #2 |
| GitHub Issues #3–#22 | 20 ticket, mỗi ticket có tiêu chí chấp nhận riêng |

Spec là nguồn chân lý về *cái gì* và *vì sao*. Ticket là nguồn chân lý về *phạm vi của lượt làm việc này*. Khi hai thứ có vẻ mâu thuẫn, hỏi trong nhóm — đừng tự đoán.

## 1. Quyết định kiến trúc bạn phải biết trước khi code

Ba điều này chi phối gần như mọi ticket. Không hiểu chúng thì sẽ viết ra code đi ngược thiết kế:

**Agent do người dùng tạo là *dữ liệu*, không phải code.** Nó là một `AgentSpec` được validate, thi hành bởi một runtime cố định do chúng ta viết. **Không sinh code, không `exec`, không bao giờ.** Đây là lý do việc kiểm soát quyền tool, sandbox, versioning và cưỡng chế ngân sách là khả thi.

**Version là bất biến, `deployed` là một con trỏ.** Sửa spec không bao giờ thay đổi bản đang chạy — nó sinh một `draft` mới. Rollback là dời con trỏ. Không có luồng nào sửa tại chỗ một version đã duyệt.

**Mức rủi ro của tool do người viết code gán, không phải LLM.** LLM chỉ được *chọn* tool trong danh mục. Nó không được tự đánh giá tool nào an toàn.

## 2. Hai team và ranh giới sở hữu

**Team A — "Sinh agent"** (2 người)
Meta-agent (phỏng vấn, sinh spec, tinh chỉnh), toàn bộ UI hướng người tạo (wizard, sơ đồ luồng), frontend shell, màn hình cấu hình admin.

**Team B — "Chạy & quản trị"** (2 người)
Schema DB + migration, runtime interpreter, implementation của tool registry, sandbox, ngân sách, eval, backend phê duyệt, deploy, chat production.

**Mặt tiếp xúc duy nhất giữa hai team: `AgentSpec` + hợp đồng REST.**
Team A *sinh ra* spec. Team B *thi hành* nó. Ngoài hai thứ đó, hai team gần như không cần biết nội bộ của nhau — và đó chính là mục đích của cách chia này.

**Không sửa vùng của team kia.** Cần một thay đổi ở đó thì nhắn, đừng tự sửa. Sửa lén là cách nhanh nhất tạo ra conflict mà không ai truy được nguồn.

## 3. Bản đồ 20 ticket

| # | Ticket | Team | Chờ ticket |
|---|---|---|---|
| **#22** | Hợp đồng: `AgentSpec` schema + tool registry | **chung** | — |
| **#3** | Hạ tầng: Postgres + Alembic + model factory + dọn code mẫu | **chung** | — |
| #4 | Đăng nhập & phân quyền hai vai trò | B | #3 |
| #5 | Viên đạn vạch đường: runtime thi hành `AgentSpec` | B | #22, #4 |
| #6 | Bộ nhớ hội thoại trong phiên | B | #5 |
| #7 | Vòng đời version: bất biến + con trỏ + rollback | B | #5 |
| #8 | Frontend scaffold + đăng nhập + cửa sổ chat | A | #4, #6 |
| #9 | Sandbox: tool phân tầng + transcript | B | #7 |
| #10 | Cưỡng chế ngân sách token/thời gian | B | #9 |
| #11 | Ba con số chi phí + trace Langfuse | B | #10 |
| #12 | Cấu hình admin: ngưỡng eval + allowlist model | A | #4, #8 |
| #13 | Eval: sinh test case, sửa, chạy, cổng ngưỡng | B | #7, #9 |
| #14 | Hàng chờ duyệt + duyệt/từ chối/vượt rào + audit | B *(A ghép)* | #8, #11, #13 |
| #15 | Deploy + chat production + API invoke + rollback | B | #8, #14 |
| #16 | Meta-agent: phỏng vấn slot-filling | A | #22, #4 |
| #17 | Sinh `AgentSpec` từ slot đã đủ | A | #7, #16 |
| #18 | Tinh chỉnh bằng chat → version nháp mới | A | #17 |
| #19 | Wizard UI: mô tả → phỏng vấn → xem spec | A | #8, #17 |
| #20 | Sơ đồ luồng React Flow — chỉ đọc | A | #19 |
| #21 | Phủ trạng thái chạy thật lên sơ đồ | A | #9, #20 |

Cạnh phụ thuộc đã khai trên GitHub bằng **native issue dependencies** — mở issue lên là thấy tab Dependencies, biết ngay nó có đang bị chặn hay không.

### Đường tới hạn

```
#3 → #4 → #5 → #7 → #9 → #10 → #11 → #14 → #15
```

Dài 9 ticket, và **nằm hoàn toàn trong Team B.** Nghĩa là dù thêm bao nhiêu người, dự án không xong nhanh hơn 9 lượt-ticket tuần tự.

Hệ quả thực tế: **Team A xong sớm thì đẩy người sang giúp Team B, không bao giờ ngược lại.**

### Ticket bỏ được

**#21** (phủ trạng thái chạy trên sơ đồ) là hạng mục cắt được nếu hết thời gian — **#20** đã là một tính năng sơ đồ trọn vẹn tự đứng được. Đừng cắt bất cứ thứ gì khác mà không họp nhóm.

## 4. Lịch theo đợt

| Đợt | Team A | Team B |
|---|---|---|
| 1 | #22 hợp đồng *(ghép chéo, 3 người)* | #22 + **#3** |
| 2 | #22 (phần tool còn lại) | **#4** |
| 3 | **#16** | **#5** |
| 4 | #16 (tiếp) | **#6** · **#7** |
| 5 | **#8** | **#9** |
| 6 | **#17** · **#12** | **#10** · **#13** |
| 7 | **#19** · **#18** | **#11** |
| 8 | **#20** | **#14** *(2 người)* |
| 9 | **#21** *(bỏ được)* | **#15** |

**Đợt 1–2 việc chia team chưa mua được gì.** `#3 → #4 → #5` là chuỗi tuần tự, và trong khoảng đó Team A không có gì độc lập để làm ngoài #22. Đừng cố lách bằng cách mở 4 ticket cho 4 người — sẽ cùng sửa những file giống nhau và mất nhiều thời gian gỡ conflict hơn là tiết kiệm được. **Hãy ghép đôi thay vì chờ.**

**#22 là ticket duy nhất cố tình trộn hai team.** Nó là hợp đồng mà cả hai bên phải cùng ký, nên ghép chéo: một người Team A viết schema + validator (họ là bên sinh ra spec, họ phải sống với schema), một người Team B viết tool registry + bản mô phỏng (họ là bên chạy tool). Người thứ hai của Team A vào giúp phần tool — sáu tool, mỗi tool cần *cả* bản thật *lẫn* bản mô phỏng, đủ việc cho ba người.

## 5. Quy ước nhánh

```
<tên>/<số issue>-<slug>
```

Ví dụ: `an/22-agentspec-contract` · `binh/3-hatang-postgres` · `chi/16-phong-van-slot`

Tiền tố tên cho biết ai đang làm và nhánh đó không ai được đụng. Phần sau cho biết nó thuộc ticket nào.

**Một nhánh = một ticket = một PR = một issue được đóng.**

Vì sao **không** dùng nhánh cá nhân dài hạn chứa nhiều ticket: đồ thị phụ thuộc của dự án này dày — **#5 chặn #6, #7, và gián tiếp chặn #8, #16.** Nếu #5 nằm trên nhánh cá nhân chưa merge thì ba người còn lại không có gì để đứng lên. Nhánh dài hạn còn khiến PR phình thành nhiều ticket trộn lẫn (review thành vô nghĩa), và không revert được *một* ticket khi nó sai.

## 6. Quy trình một ticket

```bash
# 1. Nhận ticket TRƯỚC khi viết dòng nào — để không ai vồ trùng
gh issue edit 22 --add-assignee @me

# 2. Xuất phát từ develop mới nhất
git switch develop && git pull

# 3. Nhánh mới cho đúng ticket này
git switch -c an/22-agentspec-contract
```

**4. Mở một session Claude Code MỚI** — context window sạch — rồi:

```
/implement 22
```

Nó tự đọc ticket, chạy TDD từng lát đỏ-xanh, và tự `/code-review` diff trước khi commit.

```bash
# 5. Đẩy lên và mở PR — để PR tự đóng issue khi merge
git push -u origin an/22-agentspec-contract
gh pr create --base develop --fill --body "Closes #22"

# 6. CI xanh + đã có approval thì squash merge
gh pr merge --squash --delete-branch
```

**7. Ticket sau: quay lại bước 1 với một context window MỚI.**

> **Không dùng lại context giữa hai ticket.** Đây là chỗ dễ sai nhất và nó làm chất lượng tụt rõ rệt. Mỗi ticket một session sạch, làm việc từ nội dung ticket.

## 7. Giữ nhánh đồng bộ

Nhánh ticket chưa ai dùng chung nên **rebase** cho lịch sử sạch:

```bash
git fetch origin && git rebase origin/develop
```

Làm **mỗi sáng**, và làm **ngay** khi biết một ticket chặn mình vừa merge. Có conflict thì dùng skill `/resolving-merge-conflicts`.

**Luật cứng: nhánh ticket không sống quá 2 ngày.** Quá 2 ngày nghĩa là ticket đó đáng ra phải tách nhỏ — nói ra trong nhóm để tách, đừng cố đẩy tiếp.

**Merge về `develop` hàng ngày.** Đây là thứ duy nhất giữ cho đau đớn tích hợp không dồn hết vào tuần cuối — đúng lúc không còn thời gian sửa.

## 8. Review

| PR | Người review |
|---|---|
| Bình thường | Người còn lại trong cùng team |
| **Đụng `AgentSpec` hoặc hợp đồng REST** | **Bắt buộc có người từ team kia** |

`/implement` đã tự review diff, nên review của người là lớp thứ hai: nhẹ với PR nội bộ, nghiêm với PR đụng hợp đồng.

## 9. Bốn luật giữ cho việc chia team không vỡ

**1. Một nhánh tích hợp duy nhất là `develop`.** Cả hai team cắt nhánh ticket từ `develop` và PR về `develop`. Cho mỗi team một nhánh tích hợp riêng nghe gọn nhưng nó dồn toàn bộ đau đớn tích hợp vào cuối dự án.

**2. Đổi `AgentSpec` là quyết định của cả hai team.** Đây là luật quan trọng nhất. Mọi thay đổi lên schema phải có người của cả hai team review PR. Nếu một team tự sửa hợp đồng, team kia sẽ vỡ mà không hiểu vì sao.

**3. Không sửa vùng của team kia.** Nhắn, đừng tự sửa.

**4. Sync 10 phút mỗi ngày, chỉ nói hai thứ:** có gì đổi trong `AgentSpec` không, và endpoint nào vừa xong hoặc vừa đổi. Không báo cáo tiến độ — tiến độ đã nằm trên tracker.

## 10. Hai chỗ ma sát đã biết trước

**#14 là ngoại lệ cross-team duy nhất được lên kế hoạch.** Nó thuộc Team B nhưng nặng UI, mà frontend shell lại thuộc Team A — nên cho một người Team A ghép vào. Thà nói rõ một ngoại lệ hơn là giả vờ ranh giới sạch tuyệt đối.

**#8 quyết định số phận 5 ticket sau nó.** Sau #8 có #12, #14, #15, #19, #20 đều thêm màn hình. Trong lúc làm #8, Team A **phải chốt quy ước routing và layout** để mọi màn hình sau là *file mới* thay vì sửa file dùng chung. Mười phút ở #8 tiết kiệm nhiều ngày gỡ conflict.

## 11. Khi bị chặn

**Đừng cắt nhánh từ nhánh ticket của người khác.** Đồ thị phụ thuộc tồn tại để chuyện này không xảy ra.

Nếu vẫn bị chặn, theo thứ tự ưu tiên:

1. **Ghép đôi** vào ticket đang chặn mình để nó xong nhanh hơn
2. Lấy một ticket khác đang mở trên frontier
3. Nếu thật sự không còn ticket nào mở → **dồn người vào đường tới hạn**, không phải mở việc mới ngoài kế hoạch

Xem ticket nào mở được: mở issue trên GitHub và nhìn tab Dependencies. Hoặc chạy trong **Git Bash** (PowerShell ăn dấu escape của jq):

```bash
gh api repos/Licht1906/Agent-Builder-by-Natural-Languages/issues --paginate \
  --jq '.[] | select(.state=="open" and .pull_request==null
        and .issue_dependencies_summary.blocked_by==0 and .assignee==null)
        | "#\(.number) \(.title)"'
```

## 12. Checklist trước khi mở PR

- [ ] Toàn bộ tiêu chí chấp nhận của ticket đã đạt (đánh dấu từng ô trong issue)
- [ ] Test chạy xanh **trên Postgres thật** — không phải SQLite
- [ ] Test không gọi API LLM thật: ghi đè hàm khởi tạo chat model bằng model giả
- [ ] Đã `rebase` lên `develop` mới nhất
- [ ] Không có secret / API key nào trong diff
- [ ] PR body có `Closes #<số issue>`
- [ ] PR có đụng `AgentSpec` hoặc endpoint không? Nếu có → xin review từ team kia

## 13. Tuyệt đối không

- **Không push trực tiếp vào `develop` hoặc `main`.** Mọi thứ đi qua PR.
- **Không commit `.env`** hay bất kỳ API key nào. `.env.example` là file duy nhất được commit.
- **Không sửa `.ai-log/`** — hook tự quản, tự submit lên server chấm điểm khi `git push`.
- **Không sửa hay đóng issue #2** (spec cha).
- **Không đổi `AgentSpec` một mình.**
- **Không dùng lại context window giữa hai ticket.**
- **Không dùng SQLite cho test.** Checkpointer hội thoại là bản Postgres; test trên SQLite là đang test một hệ khác với hệ chạy thật, và đúng phần dễ hỏng nhất khi deploy sẽ không được phủ ở đâu cả.

## 14. Việc cần làm một lần trước khi bắt đầu

- [ ] Tạo nhánh `develop` từ `main` làm nhánh tích hợp chung *(hiện chỉ có `develop-core`, là nhánh cá nhân)*
- [ ] Bật branch protection cho `develop`: bắt buộc PR, bắt buộc CI xanh, bắt buộc 1 approval
- [ ] Mỗi người chạy `bash scripts/setup_hooks.sh` một lần sau khi clone (cài pre-push hook để log AI usage)
- [ ] Mỗi người copy `.env.example` → `.env` và điền key riêng
- [ ] Chốt ai thuộc Team A, ai thuộc Team B — và ai là 2 người mạnh hơn cho Team B (đường tới hạn)
