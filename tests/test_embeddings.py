from listing_agent.embeddings import cosine_similarity, fit_preference_classifier


def test_cosine_similarity_ranks_matching_vectors():
    query = [1.0, 0.0]
    assert cosine_similarity(query, [1.0, 0.0]) > cosine_similarity(query, [0.0, 1.0])


def test_preference_classifier_uses_labels_and_predicts_new_embedding():
    classifier = fit_preference_classifier([
        ([1.0, 0.0], "like"),
        ([0.9, 0.1], "like"),
        ([0.0, 1.0], "dislike"),
        ([0.1, 0.9], "dislike"),
    ])
    assert classifier.predict([1.0, 0.0])[0] == "like"
    assert classifier.predict([0.0, 1.0])[0] == "dislike"


def test_preference_classifier_handles_initial_single_label_pool():
    classifier = fit_preference_classifier([([1.0, 0.0], "like")])
    assert classifier.predict([0.0, 1.0]) == ("like", 1.0)
