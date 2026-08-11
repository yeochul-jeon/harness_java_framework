# 프로젝트: {프로젝트명}

## 기술 스택
- Java {버전} (예: 17, 21)
- Spring Boot {버전} (예: 2.7.x, 3.3.x)
- 빌드툴: {Gradle Groovy DSL / Gradle Kotlin DSL / Maven}
- 영속성: {Spring Data JPA + Hibernate / MyBatis / jOOQ}
- {기타: Lombok, MapStruct, Flyway 등}
- 사내 프레임워크: {사용 시 `docs/STACK_PROFILE.md` 참조 / 미사용}

## 아키텍처 규칙
- CRITICAL: {절대 지켜야 할 규칙 1 (예: 모든 비즈니스 로직은 @Service 레이어에서만 처리. @RestController에는 라우팅/직렬화/검증만 둘 것.)}
- CRITICAL: {절대 지켜야 할 규칙 2 (예: DB 접근은 Repository 인터페이스만 사용. 컨트롤러나 서비스에서 EntityManager/SqlSession 직접 사용 금지.)}
- {일반 규칙 (예: Entity는 외부로 노출하지 말 것. 응답은 항상 DTO로 변환.)}

## 개발 프로세스
- CRITICAL: 새 기능 구현 시 반드시 테스트를 먼저 작성하고, 테스트가 통과하는 구현을 작성할 것 (TDD)
- 커밋/브랜치 규칙은 `docs/GIT.md`를 따를 것

## 명령어

> CRITICAL: 이 섹션이 빌드·테스트 커맨드의 **단일 진실원(single source of truth)** 이다.
> step의 Acceptance Criteria와 hook은 여기 적힌 커맨드를 그대로 쓴다.
> 다른 커맨드를 임의로 지어내지 마라. 이 섹션이 비어 있거나 틀렸다고 판단되면
> 작업을 진행하지 말고 `/bootstrap`을 실행하라고 사용자에게 알려라.

```
{실행}      # 예: ./gradlew bootRun   |  ./mvnw spring-boot:run
{빌드}      # 예: ./gradlew build     |  ./mvnw verify
{테스트}    # 예: ./gradlew test      |  ./mvnw test
{컴파일}    # 예: ./gradlew compileJava compileTestJava  |  ./mvnw -q test-compile
```

- **빌드**: step 완료 시 AC로 실행하는 전체 검증 커맨드.
- **컴파일**: Stop hook이 매 응답마다 돌리는 가벼운 검증 커맨드. 테스트 실행을 포함하지 않는다.
